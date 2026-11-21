// Jarvis in the MacBook notch: a small always-on panel that mirrors what Jarvis (copilot_web.py) is doing.
// It talks only to Jarvis on 127.0.0.1:8765, and takes keyboard focus only while you type into it.
// Build: zsh app/install_notch.sh   Try states without Jarvis: JarvisNotch --demo [n]
import AppKit
import Carbon
import SwiftUI

let BASE = ProcessInfo.processInfo.environment["JARVIS_URL"] ?? "http://127.0.0.1:8765"
let blue = Color(red: 0.23, green: 0.51, blue: 0.96)
let amber = Color(red: 0.98, green: 0.75, blue: 0.24)
let green = Color(red: 0.29, green: 0.85, blue: 0.5)
let muted = Color(white: 0.62)
let red = Color(red: 0.97, green: 0.42, blue: 0.4)

struct Draft: Equatable { var channel = "", to = "", subject = "", message = "" }

struct JTask: Identifiable, Equatable {
    let id: Int
    var title: String, state: String, reply: String, waiting: String
    var draft: Draft?

    init(id: Int, title: String, state: String, reply: String = "", waiting: String = "", draft: Draft? = nil) {
        self.id = id; self.title = title; self.state = state; self.reply = reply; self.waiting = waiting; self.draft = draft
    }

    init(_ d: [String: Any]) {
        let auto = d["auto"] as? String ?? ""
        self.init(id: d["tid"] as? Int ?? 0, title: auto.isEmpty ? d["text"] as? String ?? "" : auto,
                  state: d["state"] as? String ?? "", reply: d["reply"] as? String ?? "", waiting: d["waiting"] as? String ?? "")
        if let x = d["draft"] as? [String: Any], let msg = x["message"] as? String, !msg.isEmpty {
            draft = Draft(channel: x["channel"] as? String ?? "", to: x["to"] as? String ?? "",
                          subject: x["subject"] as? String ?? "", message: msg)
        }
    }
}

enum Phase: Equatable { case idle, peek, listening, thinking, speaking, typing, ask, banner }

struct Banner: Equatable { var title: String, text: String, color: Color }

func trim(_ s: String) -> String { s.trimmingCharacters(in: .whitespacesAndNewlines) }

func firstLine(_ s: String) -> String {
    s.split(whereSeparator: \.isNewline).first.map(String.init) ?? s
}

final class Model: ObservableObject {
    @Published var connected = false
    @Published var voicePhase: Phase = .idle
    @Published var heard = ""
    @Published var said = ""
    @Published var level: Double = 0
    @Published var tasks: [JTask] = []
    @Published var banner: Banner?
    @Published var hover = false
    @Published var pinned = false
    @Published var voiceWorking = false
    @Published var typing = false
    @Published var input = ""
    @Published var askID: Int?
    @Published var edit = ""
    var keyActive = false, hotkey = false
    var onKey: ((Bool) -> Void)?
    var shapeSize = CGSize(width: 260, height: 32)
    var notchW: CGFloat = 185, notchH: CGFloat = 32
    var demo = false
    private var collapse: DispatchWorkItem?, bannerEnd: DispatchWorkItem?, pinEnd: DispatchWorkItem?, hoverStart: DispatchWorkItem?, askEnd: DispatchWorkItem?
    private var known: [Int: String] = [:]
    private var loaded = false, refreshing = false, again = false

    var needsYou: [JTask] { tasks.filter { $0.state == "needs_ok" || $0.state == "checkin" } }
    var working: [JTask] { tasks.filter { $0.state == "working" || $0.state == "queued" } }
    var busy: Bool { !working.isEmpty || voiceWorking }
    var ask: JTask? { askID.flatMap { id in needsYou.first { $0.id == id } } }
    var phase: Phase {
        if voicePhase != .idle { return voicePhase }
        if typing { return .typing }
        if ask != nil { return .ask }
        if banner != nil { return .banner }
        return hover || pinned ? .peek : .idle
    }

    // ---- Jarvis's live feed (/api/events, one JSON object per line)

    func start() {
        Task.detached { await self.stream() }
        Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { _ in self.refresh() }
    }

    func stream() async {
        while true {
            do {
                var req = URLRequest(url: URL(string: BASE + "/api/events")!)
                req.setValue("1", forHTTPHeaderField: "X-Mac-Copilot")
                req.timeoutInterval = 60
                let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                await MainActor.run { self.connected = true; self.refresh() }
                for try await line in bytes.lines {
                    guard let d = line.data(using: .utf8),
                          let e = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { continue }
                    await MainActor.run { self.handle(e) }
                }
            } catch {}
            await MainActor.run { self.connected = false; self.voicePhase = .idle }
            try? await Task.sleep(nanoseconds: 3_000_000_000)
        }
    }

    func handle(_ e: [String: Any]) {
        let src = e["src"] as? String ?? "", type = e["type"] as? String ?? ""
        if src == "voice" { voice(type, e) }
        if src == "task" && !["tool", "helper", "followup", "route"].contains(type) { refresh() }
    }

    func voice(_ type: String, _ e: [String: Any]) {
        let on = e["on"] as? Bool ?? false
        let text = (e["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        switch type {
        case "listening":
            if on { stopCollapse(); heard = ""; said = ""; level = 0; voicePhase = .listening }
            else { voicePhase = e["sent"] as? Bool == true ? .thinking : .idle }
        case "level": level = (e["v"] as? NSNumber)?.doubleValue ?? 0
        case "heard":
            if !text.isEmpty { heard = text; if voicePhase != .speaking { voicePhase = .thinking } }
        case "said":
            if !text.isEmpty { said = text; voicePhase = .speaking; later(8) }
        case "speaking":
            if on { stopCollapse(); if voicePhase != .listening { voicePhase = .speaking } } else { later(4) }
        case "task": voiceWorking = true
        case "end", "stopped":
            voiceWorking = false
            if voicePhase == .thinking { later(2) }
        default: break
        }
    }

    func later(_ s: Double) {
        stopCollapse()
        let w = DispatchWorkItem { [weak self] in self?.voicePhase = .idle }
        collapse = w
        DispatchQueue.main.asyncAfter(deadline: .now() + s, execute: w)
    }

    func stopCollapse() { collapse?.cancel(); collapse = nil }

    func refresh() {
        if demo { return }
        if refreshing { again = true; return }
        refreshing = true
        var req = URLRequest(url: URL(string: BASE + "/api/tasks")!)
        req.setValue("1", forHTTPHeaderField: "X-Mac-Copilot")
        URLSession.shared.dataTask(with: req) { data, _, _ in
            let list = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [[String: Any]] }
            DispatchQueue.main.async {
                self.refreshing = false
                if let list { self.apply(list.map(JTask.init)) }
                if self.again { self.again = false; self.refresh() }
            }
        }.resume()
    }

    func apply(_ list: [JTask]) {
        if loaded {
            for t in list where known[t.id] != t.state {
                let say = t.reply.isEmpty ? (t.waiting.isEmpty ? t.title : t.waiting) : t.reply
                switch t.state {
                case "needs_ok", "checkin": openAsk(t)
                case "done": show(Banner(title: "Done", text: firstLine(say), color: green), 5)
                case "error": show(Banner(title: "Couldn't finish", text: t.title, color: .red), 6)
                default: break
                }
            }
        }
        known = Dictionary(list.map { ($0.id, $0.state) }, uniquingKeysWith: { $1 })
        loaded = true
        tasks = list
        if askID != nil && ask == nil { closeAsk() }
    }

    // ---- answering from the notch: drafts (Yes / Send my edit / No), check-ins, typed requests

    func openAsk(_ t: JTask) {
        unpin()
        if askID != t.id { edit = t.draft?.message ?? "" }
        askID = t.id
        banner = nil
        armAsk()
    }

    // The card folds back into the ears after 15 s, unless you're pointing at it or typing in it.
    func armAsk() {
        askEnd?.cancel()
        let w = DispatchWorkItem { [weak self] in
            guard let s = self else { return }
            if s.hover || s.keyActive { s.armAsk() } else { s.askID = nil }
        }
        askEnd = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: w)
    }

    func closeAsk() {
        askEnd?.cancel()
        askID = nil
        if !typing { onKey?(false) }
    }

    func approve(_ t: JTask) {
        var body: [String: Any] = ["text": "Yes, go ahead", "task": t.id]
        if let d = t.draft, !trim(edit).isEmpty, trim(edit) != trim(d.message) { body["edit"] = trim(edit) }
        closeAsk()
        post("/api/command", body)
    }

    func decline(_ t: JTask) {
        closeAsk()
        post("/api/command", ["text": "No, cancel", "task": t.id])
    }

    func answerCheckin(_ t: JTask, _ answer: String) {
        closeAsk()
        post("/api/checkin", ["task": t.id, "answer": answer])
    }

    func startTyping() {
        banner = nil
        input = ""
        typing = true
        if !demo { onKey?(true) }
    }

    func stopTyping() {
        typing = false
        onKey?(false)
    }

    func submit() {
        let text = trim(input)
        stopTyping()
        guard !text.isEmpty else { return }
        post("/api/command", ["text": text, "front": true])
        show(Banner(title: "Asked", text: text, color: blue), 3)
    }

    func openPage() {
        if typing { stopTyping() }
        NSWorkspace.shared.open(URL(string: BASE)!)
    }

    func post(_ path: String, _ body: [String: Any]) {
        if demo { return }
        var req = URLRequest(url: URL(string: BASE + path)!)
        req.httpMethod = "POST"
        req.setValue("1", forHTTPHeaderField: "X-Mac-Copilot")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            let r = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async {
                if let err = r?["error"] as? String { self.show(Banner(title: "Not done", text: err, color: red), 6) }
                else if !ok { self.show(Banner(title: "Not done", text: "Couldn't reach Jarvis.", color: red), 6) }
                self.refresh()
            }
        }.resume()
    }

    func show(_ b: Banner, _ secs: Double) {
        banner = b
        bannerEnd?.cancel()
        let w = DispatchWorkItem { [weak self] in self?.banner = nil }
        bannerEnd = w
        DispatchQueue.main.asyncAfter(deadline: .now() + secs, execute: w)
    }

    func setHover(_ inside: Bool) {
        if demo || inside == hover { if !inside { hoverStart?.cancel() }; return }
        if !inside { hoverStart?.cancel(); hover = false; return }
        guard hoverStart == nil || hoverStart!.isCancelled else { return }
        let w = DispatchWorkItem { [weak self] in self?.hover = true; self?.hoverStart = nil }
        hoverStart = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: w)
    }

    // Click: first opens the list of what's waiting (one line each, tap one to answer), then types.
    func click() {
        if demo { return }
        if voicePhase == .speaking { voicePhase = .idle; return }
        if !pinned && !needsYou.isEmpty { banner = nil; pin(); return }
        if banner != nil { banner = nil; openPage(); return }
        unpin()
        startTyping()
    }

    func pin() {
        pinned = true
        pinEnd?.cancel()
        let w = DispatchWorkItem { [weak self] in
            guard let s = self else { return }
            if s.hover { s.pin() } else { s.pinned = false }
        }
        pinEnd = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 20, execute: w)
    }

    func unpin() { pinEnd?.cancel(); pinned = false }
}

// ---- views

// The Jarvis logo (web/logo.svg): a glowing core with three rings; the front of the first ring passes over the core.
struct Logo: View {
    var size: CGFloat
    var live = false
    var dim = false
    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: !live)) { ctx in
            let spin = live ? sin(ctx.date.timeIntervalSinceReferenceDate * 2.4) * 10 : 0
            Canvas { c, sz in
                c.translateBy(x: sz.width / 2, y: sz.height / 2)
                c.scaleBy(x: sz.width / 100, y: sz.height / 100)
                let light = Color(red: 0.5, green: 0.86, blue: 1), mid = Color(red: 0.12, green: 0.55, blue: 1), deep = Color(red: 0.04, green: 0.25, blue: 0.75)
                func ring(_ deg: Double, _ rx: CGFloat, _ ry: CGFloat, _ cx: CGFloat, _ cy: CGFloat, _ irx: CGFloat, _ iry: CGFloat, front: Bool = false) {
                    var g = c
                    g.rotate(by: .degrees(deg + spin))
                    if front { g.clip(to: Path(CGRect(x: -60, y: 0, width: 120, height: 60))) }
                    var p = Path(ellipseIn: CGRect(x: -rx, y: -ry, width: 2 * rx, height: 2 * ry))
                    p.addPath(Path(ellipseIn: CGRect(x: cx - irx, y: cy - iry, width: 2 * irx, height: 2 * iry)))
                    g.fill(p, with: .linearGradient(Gradient(colors: [light, mid]), startPoint: CGPoint(x: -rx, y: -ry), endPoint: CGPoint(x: rx, y: ry)), style: FillStyle(eoFill: true))
                }
                ring(-28, 47, 14, 2.8, -3.6, 43.4, 6)
                ring(32, 45, 13, 2.62, -3.38, 41.62, 5.5)
                ring(92, 44, 13, 2.62, -3.38, 40.62, 5.5)
                c.fill(Path(ellipseIn: CGRect(x: -17, y: -17, width: 34, height: 34)),
                       with: .radialGradient(Gradient(colors: [light, mid, deep]), center: CGPoint(x: -4.8, y: -6.1), startRadius: 0, endRadius: 25.5))
                ring(-28, 47, 14, 2.8, -3.6, 43.4, 6, front: true)
            }
            .frame(width: size, height: size)
            .saturation(dim ? 0 : 1)
            .opacity(dim ? 0.55 : 1)
            .shadow(color: dim ? .clear : Color(red: 0.3, green: 0.65, blue: 1).opacity(live ? 0.9 : 0.45), radius: live ? size * 0.35 : size * 0.18)
        }
    }
}

struct Bars: View {
    var level: Double
    private let shape: [Double] = [0.5, 0.85, 1, 0.7, 0.45]
    var body: some View {
        HStack(spacing: 2.5) {
            ForEach(0..<5, id: \.self) { i in
                Capsule().fill(Color(red: 0.45, green: 0.75, blue: 1))
                    .frame(width: 3, height: 4 + 14 * CGFloat(min(1, level * 1.4) * shape[i]))
            }
        }
        .frame(height: 18)
        .animation(.easeOut(duration: 0.12), value: level)
    }
}

struct Spinner: View {
    var body: some View { ProgressView().controlSize(.mini).progressViewStyle(.circular) }
}

struct Pill: View {
    var title: String
    var primary = false
    var action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 12, weight: .semibold))
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(Capsule().fill(primary ? blue : Color(white: 0.2)))
        }
        .buttonStyle(.plain)
    }
}

struct NotchView: View {
    @EnvironmentObject var m: Model
    @FocusState private var focus: Bool

    var body: some View {
        let p = m.phase
        let open = p != .idle
        let earW: CGFloat = m.busy ? 92 : 38
        let w: CGFloat = open ? 520 : m.notchW + 2 * earW
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                left(p).frame(maxWidth: .infinity, alignment: .leading).padding(.leading, open ? 18 : 12)
                Color.clear.frame(width: m.notchW)
                right(p).frame(maxWidth: .infinity, alignment: .trailing).padding(.trailing, open ? 18 : 12)
            }
            .frame(height: m.notchH)
            if open {
                content(p)
                    .padding(.horizontal, 20).padding(.top, 8).padding(.bottom, 16)
                    .frame(width: w, alignment: .leading)
                    .transition(.opacity)
            }
        }
        .frame(width: w)
        .background(UnevenRoundedRectangle(bottomLeadingRadius: open ? 22 : 11, bottomTrailingRadius: open ? 22 : 11, style: .continuous).fill(Color.black))
        .onGeometryChange(for: CGSize.self) { $0.size } action: { if $0.width > 0 { m.shapeSize = $0 } }
        .contentShape(Rectangle())
        .gesture(TapGesture().onEnded { m.click() }, including: [.idle, .peek, .banner, .speaking].contains(p) ? .all : .subviews)
        .onChange(of: m.typing) { _, on in
            if on { DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { focus = true } }
        }
        .animation(.spring(response: 0.36, dampingFraction: 0.84), value: p)
        .animation(.spring(response: 0.36, dampingFraction: 0.84), value: m.busy)
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .environment(\.colorScheme, .dark)
    }

    @ViewBuilder func left(_ p: Phase) -> some View {
        if p != .idle {
            HStack(spacing: 7) {
                Logo(size: 20, live: p == .listening || p == .speaking, dim: !m.connected)
                Text("Jarvis").font(.system(size: 13, weight: .semibold))
            }
        } else {
            HStack(spacing: 6) {
                if !m.needsYou.isEmpty {
                    Circle().fill(amber).frame(width: 7, height: 7)
                    Text("\(m.needsYou.count)").font(.system(size: 12, weight: .semibold)).foregroundStyle(amber)
                }
                if m.busy {
                    Spinner()
                    if m.needsYou.isEmpty { Text("Working").font(.system(size: 12)).foregroundStyle(muted) }
                }
            }
        }
    }

    @ViewBuilder func right(_ p: Phase) -> some View {
        switch p {
        case .idle:
            HStack(spacing: 6) {
                if m.busy, m.working.count > 1 { Text("\(m.working.count)").font(.system(size: 12)).foregroundStyle(muted) }
                Logo(size: 20, dim: !m.connected)
            }
        case .listening: Bars(level: m.level)
        case .thinking: HStack(spacing: 6) { Spinner(); Text("Thinking").font(.system(size: 12)).foregroundStyle(muted) }
        case .speaking: Text("Speaking").font(.system(size: 12)).foregroundStyle(muted)
        case .typing: Button("Open Jarvis ↗") { m.openPage() }.buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(muted)
        case .ask:
            let check = m.ask?.state == "checkin"
            Text(check ? "Check-in" : "Needs your OK").font(.system(size: 12, weight: .semibold)).foregroundStyle(check ? blue : amber)
        case .banner: Text(m.banner?.title ?? "").font(.system(size: 12, weight: .semibold)).foregroundStyle(m.banner?.color ?? muted)
        case .peek:
            HStack(spacing: 10) {
                Text(m.pinned ? "\(m.needsYou.count) need you" : "\(m.working.count) working · \(m.needsYou.count) need you")
                    .font(.system(size: 12)).foregroundStyle(muted).lineLimit(1).fixedSize()
                if m.pinned { Button("Open ↗") { m.unpin(); m.openPage() }.buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(muted) }
            }
        }
    }

    @ViewBuilder func content(_ p: Phase) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            switch p {
            case .listening:
                Text("Listening…").font(.system(size: 15, weight: .medium))
                foot("Let go of ⌥⌘ when you're done")
            case .thinking:
                Text("“\(m.heard.isEmpty ? "…" : m.heard)”").font(.system(size: 14)).lineLimit(2)
                foot("Working on it")
            case .speaking:
                if !m.heard.isEmpty { Text(m.heard).font(.system(size: 12)).foregroundStyle(muted).lineLimit(1) }
                Text(m.said.isEmpty ? "…" : m.said).font(.system(size: 14)).lineLimit(4).fixedSize(horizontal: false, vertical: true)
            case .typing:
                TextField("Ask Jarvis anything…", text: $m.input)
                    .textFieldStyle(.plain).font(.system(size: 15))
                    .focused($focus)
                    .onSubmit { m.submit() }
                    .onExitCommand { m.stopTyping() }
                foot("Return to send · Esc to close · Jarvis sees which app you're in")
            case .ask:
                if let t = m.ask { askCard(t) }
            case .banner:
                Text(m.banner?.text ?? "").font(.system(size: 14)).lineLimit(3).fixedSize(horizontal: false, vertical: true)
                foot("Click to open Jarvis")
            case .peek:
                let rows = m.needsYou + m.working
                if rows.isEmpty {
                    Text(m.connected ? "Nothing running. Hold ⌥⌘ to talk." : "Jarvis isn't running.").font(.system(size: 14))
                } else {
                    ForEach(rows.prefix(m.pinned ? 6 : 4)) { t in
                        row(t).contentShape(Rectangle()).onTapGesture { if m.needsYou.contains(t) { m.openAsk(t) } else { m.openPage() } }
                    }
                }
                if rows.count > (m.pinned ? 6 : 4) { foot("+\(rows.count - (m.pinned ? 6 : 4)) more · Open ↗ for all") }
                foot(m.needsYou.isEmpty || m.pinned ? (m.needsYou.isEmpty ? "" : "Tap one to answer · ") + (m.hotkey ? "Click or press ⌥Space to type" : "Click to type")
                     : "Click to see what's waiting")
            case .idle: EmptyView()
            }
        }
    }

    @ViewBuilder func askCard(_ t: JTask) -> some View {
        let more = m.needsYou.count - 1
        Text(t.title).font(.system(size: 12)).foregroundStyle(muted).lineLimit(1)
        if t.state == "checkin" {
            Text(t.reply.isEmpty ? t.title : t.reply).font(.system(size: 14)).lineLimit(3).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Pill(title: "Keep", primary: true) { m.answerCheckin(t, "keep") }
                Pill(title: "Snooze") { m.answerCheckin(t, "snooze") }
                Pill(title: "Drop") { m.answerCheckin(t, "drop") }
                Spacer()
                if more > 0 { Text("+\(more) more").font(.system(size: 11)).foregroundStyle(muted) }
            }.padding(.top, 4)
        } else if let d = t.draft {
            Text((["To \(d.to)", d.channel, d.subject].filter { !$0.isEmpty }).joined(separator: " · ") + " · edit before sending")
                .font(.system(size: 11)).foregroundStyle(muted).lineLimit(1)
            TextField("", text: $m.edit, axis: .vertical)
                .textFieldStyle(.plain).font(.system(size: 13)).lineLimit(1...5)
                .padding(8).background(RoundedRectangle(cornerRadius: 8).fill(Color(white: 0.13)))
                .onExitCommand { m.closeAsk() }
            HStack(spacing: 8) {
                Pill(title: trim(m.edit) != trim(d.message) && !trim(m.edit).isEmpty ? "Send my edit" : "Yes, send", primary: true) { m.approve(t) }
                Pill(title: "No") { m.decline(t) }
                Spacer()
                if more > 0 { Text("+\(more) more").font(.system(size: 11)).foregroundStyle(muted) }
            }.padding(.top, 4)
        } else {
            Text(t.reply.isEmpty ? (t.waiting.isEmpty ? t.title : t.waiting) : t.reply)
                .font(.system(size: 14)).lineLimit(4).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Pill(title: "Yes", primary: true) { m.approve(t) }
                Pill(title: "No") { m.decline(t) }
                Spacer()
                if more > 0 { Text("+\(more) more").font(.system(size: 11)).foregroundStyle(muted) }
            }.padding(.top, 4)
        }
    }

    func row(_ t: JTask) -> some View {
        let label = ["needs_ok": "needs your OK", "checkin": "check-in", "queued": "waiting its turn"][t.state] ?? "working"
        let needs = t.state == "needs_ok" || t.state == "checkin"
        return HStack(spacing: 8) {
            if needs { Circle().fill(amber).frame(width: 7, height: 7).frame(width: 14) } else { Spinner().frame(width: 14) }
            Text(oneLine(t)).font(.system(size: 13)).lineLimit(1)
            Spacer(minLength: 8)
            Text(label).font(.system(size: 11, weight: needs ? .semibold : .regular)).foregroundStyle(needs ? amber : muted)
        }
    }

    func oneLine(_ t: JTask) -> String {
        if let d = t.draft { return "\(d.channel.capitalized) \(d.to): \(firstLine(d.message))" }
        if let r = try? Regex(#"^You haven't touched “(.+)” in ([^.?]+)"#), let x = t.reply.firstMatch(of: r),
           let what = x.output[1].substring, let age = x.output[2].substring {
            return "\(what) · \(age) quiet"
        }
        if t.state == "checkin" || t.state == "needs_ok" { return firstLine(t.reply.isEmpty ? t.title : t.reply) }
        return t.title
    }

    func foot(_ s: String) -> some View {
        Text(s).font(.system(size: 11)).foregroundStyle(muted).padding(.top, 2)
    }
}

// ---- demo states, for screenshots without touching Jarvis

func demo(_ m: Model, _ i: Int) {
    let ask = JTask(id: 1, title: "Slack Taylor the partner deck link", state: "needs_ok")
    let a = JTask(id: 2, title: "Find the AngelList wire email", state: "working")
    let b = JTask(id: 3, title: "What's on my calendar Thursday?", state: "working")
    m.connected = true; m.banner = nil; m.hover = false; m.voicePhase = .idle; m.level = 0.7; m.typing = false; m.askID = nil; m.pinned = false
    m.heard = "What's on my calendar this afternoon?"
    m.said = "Two things: partner sync with Taylor at 2, and the project review at 4:30. You're free after 5."
    let draft = JTask(id: 4, title: "Slack Taylor the partner deck link", state: "needs_ok", reply: "Send it?",
                      draft: Draft(channel: "Slack", to: "Taylor", message: "Here's the partner deck: example.invalid/deck. Let me know what you think!"))
    let check = JTask(id: 5, title: "Check-in", state: "checkin", reply: "You said you'd send Lisa the invoice today. Still on it?")
    switch i % 12 {
    case 11:
        m.tasks = [check, JTask(id: 6, title: "Check-in", state: "checkin", reply: "Devin's been quiet for 7 hours on the invite page. Keep, snooze or drop?"),
                   JTask(id: 7, title: "Page watch", state: "checkin", reply: "Page watch: AirPods Pro 3 are $199 at Best Buy (was $249)."), a]
        m.pinned = true
    case 8: m.tasks = [draft, a]; m.edit = draft.draft!.message; m.askID = 4
    case 9: m.tasks = [check]; m.askID = 5
    case 10: m.tasks = [a]; m.typing = true; m.input = "What's the context here?"
    case 0: m.tasks = [ask]
    case 1: m.tasks = [a, b]
    case 2: m.tasks = []; m.voicePhase = .listening
    case 3: m.voicePhase = .thinking
    case 4: m.voicePhase = .speaking
    case 5: m.tasks = [ask, a]; m.banner = Banner(title: "Needs your OK", text: "Slack Taylor: “Here's the partner deck link.” Send it?", color: amber)
    case 6: m.tasks = [ask, a, b]; m.hover = true
    default: m.tasks = []; m.banner = Banner(title: "Check-in", text: "You said you'd send Lisa the invoice today. Done yet?", color: blue)
    }
}

// ---- the panel

final class HostView<C: View>: NSHostingView<C> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// Becomes key only while you type (a text field), and never makes Jarvis Notch the active app.
final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

var hotKeyAction: (() -> Void)?

// ⌥Space anywhere opens the notch for typing (Carbon hot keys need no extra permission).
func registerHotKey() -> Bool {
    var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
    InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in hotKeyAction?(); return noErr }, 1, &spec, nil, nil)
    var ref: EventHotKeyRef?
    let id = EventHotKeyID(signature: OSType(0x4A4E4F54), id: 1)
    return RegisterEventHotKey(UInt32(kVK_Space), UInt32(optionKey), id, GetApplicationEventTarget(), 0, &ref) == noErr
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = Model()
    var panel: NSPanel!
    let size = NSSize(width: 640, height: 340)

    func applicationDidFinishLaunching(_ note: Notification) {
        let args = CommandLine.arguments
        panel = KeyPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.becomesKeyOnlyIfNeeded = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.mainMenuWindow)) + 3)
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        panel.isMovable = false
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = true
        panel.contentView = HostView(rootView: NotchView().environmentObject(model))
        place()
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in self?.place() }
        NotificationCenter.default.addObserver(forName: NSWindow.didBecomeKeyNotification, object: panel, queue: .main) { [weak self] _ in self?.model.keyActive = true }
        NotificationCenter.default.addObserver(forName: NSWindow.didResignKeyNotification, object: panel, queue: .main) { [weak self] _ in
            guard let m = self?.model else { return }
            m.keyActive = false
            m.typing = false
        }
        model.onKey = { [weak self] on in on ? self?.panel.makeKey() : self?.giveBack() }

        if let i = args.firstIndex(of: "--demo") {
            model.demo = true
            if i + 1 < args.count, let n = Int(args[i + 1]) { demo(model, n) } else {
                var n = 0
                demo(model, n)
                Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in n += 1; demo(self.model, n) }
            }
        } else {
            hotKeyAction = { [weak self] in
                guard let m = self?.model else { return }
                m.typing ? m.stopTyping() : m.startTyping()
            }
            model.hotkey = registerHotKey()
            model.start()
        }
        let t = Timer(timeInterval: 0.05, repeats: true) { [weak self] _ in self?.track() }
        RunLoop.main.add(t, forMode: .common)
    }

    // Only the MacBook's built-in screen has a notch (safe-area inset at the top); with the lid closed, hide.
    func place() {
        guard let s = NSScreen.screens.first(where: { $0.safeAreaInsets.top > 0 }) else { panel.orderOut(nil); return }
        model.notchH = s.safeAreaInsets.top
        let l = s.auxiliaryTopLeftArea?.width ?? 0, r = s.auxiliaryTopRightArea?.width ?? 0
        model.notchW = l > 0 && r > 0 ? s.frame.width - l - r : 185
        panel.setFrame(NSRect(x: s.frame.midX - size.width / 2, y: s.frame.maxY - size.height, width: size.width, height: size.height), display: true)
        panel.orderFrontRegardless()
    }

    // Hand the keyboard back to the app you were in (it never stopped being the active app).
    func giveBack() {
        guard panel.isKeyWindow else { return }
        panel.orderOut(nil)
        panel.orderFrontRegardless()
        NSWorkspace.shared.frontmostApplication?.activate()
    }

    // The panel is bigger than the black shape; let clicks through everywhere outside the shape.
    func track() {
        guard panel.isVisible else { return }
        let f = panel.frame, s = model.shapeSize, p = NSEvent.mouseLocation
        let shape = NSRect(x: f.midX - s.width / 2, y: f.maxY - s.height, width: s.width, height: s.height)
        let inside = shape.contains(p)
        if panel.ignoresMouseEvents == inside { panel.ignoresMouseEvents = !inside }
        model.setHover(inside)
    }
}

@main
struct JarvisNotchApp {
    static func main() {
        let app = NSApplication.shared
        let d = AppDelegate()
        app.delegate = d
        app.setActivationPolicy(.accessory)
        app.run()
    }
}

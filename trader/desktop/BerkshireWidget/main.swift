// 波克夏紙上交易：桌面小儀表板（M6d，方案 B：SwiftUI 浮動視窗，固定在桌面層）。
// 資料只讀本機 API（127.0.0.1）；同意按鈕帶 state/.dashboard_token。點按鈕以外的地方開網頁；拖曳可移動。純黑底，不用毛玻璃。
// 參數：--state <state 目錄>（必填，讀權杖）、--port <埠>（預設 8787）。
import AppKit
import SwiftUI

// ---------------------------------------------------------------- 參數
struct Options {
    var stateDir: String = ""
    var port: Int = 8787
    init() {
        var it = CommandLine.arguments.dropFirst().makeIterator()
        while let a = it.next() {
            if a == "--state", let v = it.next() { stateDir = v }
            if a == "--port", let v = it.next(), let p = Int(v) { port = p }
        }
    }
    var base: URL { URL(string: "http://127.0.0.1:\(port)")! }
    func token() -> String? {
        guard !stateDir.isEmpty else { return nil }
        let p = (stateDir as NSString).expandingTildeInPath + "/.dashboard_token"
        return (try? String(contentsOfFile: p, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
let options = Options()

// ---------------------------------------------------------------- 資料
struct Top: Decodable { let ticker: String; let weight: Double?; let `return`: Double? }
struct PnL: Decodable { let total: Double }
struct Approvals: Decodable { let n_pending: Int; let est_input_tokens: Int; let est_minutes: Double; let label: String? }
struct Summary: Decodable {
    let empty: Bool
    let as_of: String?
    let `return`: Double?
    let spy_return: Double?
    let excess_pp: Double?
    let pnl: PnL?
    let cash_ratio: Double?
    let nav: Double?
    let n_positions: Int?
    let top3: [Top]?
    let approvals: Approvals
}
struct NavPoint: Decodable { let date: String; let account: Double?; let spy: Double? }
struct Nav: Decodable { let points: [NavPoint] }

@MainActor
final class Model: ObservableObject {
    @Published var summary: Summary?
    @Published var points: [NavPoint] = []
    @Published var error: String?
    @Published var busy = false

    func refresh() async {
        let d1: Data
        do {
            (d1, _) = try await URLSession.shared.data(from: options.base.appendingPathComponent("api/summary"))
        } catch {
            self.error = "儀表板伺服器未回應"
            return
        }
        do {
            let s = try JSONDecoder().decode(Summary.self, from: d1)
            var c = URLComponents(url: options.base.appendingPathComponent("api/nav"), resolvingAgainstBaseURL: false)!
            c.queryItems = [URLQueryItem(name: "range", value: "3M")]
            let (d2, _) = try await URLSession.shared.data(from: c.url!)
            points = (try? JSONDecoder().decode(Nav.self, from: d2).points) ?? []
            summary = s
            error = nil
        } catch {
            self.error = "資料格式無法解讀：\(error.localizedDescription)"
        }
    }

    func approveAll() async -> String {
        guard let tok = options.token() else { return "找不到權杖檔（--state）" }
        var r = URLRequest(url: options.base.appendingPathComponent("api/approvals/approve"))
        r.httpMethod = "POST"
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.setValue(tok, forHTTPHeaderField: "X-Dashboard-Token")
        r.httpBody = try? JSONSerialization.data(withJSONObject: ["all": true, "via": "desktop"])
        busy = true
        defer { busy = false }
        do {
            let (d, resp) = try await URLSession.shared.data(for: r)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
            await refresh()
            if code != 200 { return "同意失敗（\(code)）：\(j?["detail"] ?? "")" }
            let n = (j?["approved"] as? [Any])?.count ?? 0
            return (j?["started"] as? Bool ?? false) ? "已同意 \(n) 檔，背景執行中" : "已同意 \(n) 檔（\(j?["note"] ?? "")）"
        } catch {
            return "同意失敗：伺服器未回應"
        }
    }
}

// ---------------------------------------------------------------- 格式
func pct(_ x: Double?, _ d: Int = 2, sign: Bool = true) -> String {
    guard let x = x else { return "—" }
    return String(format: "%@%.\(d)f%%", sign && x > 0 ? "+" : (x < 0 ? "−" : ""), abs(x) * 100)
}
func pp(_ x: Double?) -> String {
    guard let x = x else { return "—" }
    return String(format: "%@%.2f pp", x > 0 ? "+" : (x < 0 ? "−" : ""), abs(x))
}
func usd(_ x: Double?, sign: Bool = true) -> String {
    guard let x = x else { return "—" }
    let f = NumberFormatter(); f.numberStyle = .decimal; f.maximumFractionDigits = 0
    let s = f.string(from: NSNumber(value: abs(x))) ?? "\(abs(x))"
    return "\(sign && x > 0 ? "+" : x < 0 ? "−" : "")$\(s)"
}
func rgb(_ hex: UInt32, _ a: Double = 1) -> Color {
    Color(.sRGB, red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
          blue: Double(hex & 0xFF) / 255, opacity: a)
}
// 純黑底；介面黑白灰，顏色只留給漲跌；唯一強調色（陶土橘）只用於同意按鈕
let ink = rgb(0xF5F5F7), ink2 = rgb(0x98989F), ink3 = rgb(0x5C5C63)
let hairline = Color.white.opacity(0.08)
let upColor = rgb(0x30D158), downColor = rgb(0xFF453A), clay = rgb(0xD97757)
func tone(_ x: Double?) -> Color { guard let x = x, x != 0 else { return ink }; return x > 0 ? upColor : downColor }
func display(_ size: CGFloat, _ w: Font.Weight = .regular) -> Font { .system(size: size, weight: w, design: .default).monospacedDigit() }

// ---------------------------------------------------------------- 走勢圖
struct Spark: View {
    let points: [NavPoint]
    let color: Color
    var body: some View {
        GeometryReader { g in
            let vals = points.flatMap { [$0.account, $0.spy] }.compactMap { $0 }
            if vals.count < 2 {
                Text("尚無走勢").font(.system(size: 11)).foregroundStyle(ink3).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                let lo = vals.min()!, hi = max(vals.max()!, lo + 0.5)
                let pad = (hi - lo) * 0.12
                let n = points.count
                let w = g.size.width - 6, h = g.size.height
                let x = { (i: Int) -> CGFloat in n == 1 ? w / 2 : CGFloat(i) / CGFloat(n - 1) * w }
                let y = { (v: Double) -> CGFloat in (1 - CGFloat((v - lo + pad) / (hi - lo + 2 * pad))) * h }
                let line = { (k: KeyPath<NavPoint, Double?>) -> Path in
                    var p = Path(); var started = false
                    for (i, pt) in points.enumerated() {
                        guard let v = pt[keyPath: k] else { started = false; continue }
                        let c = CGPoint(x: x(i), y: y(v))
                        if started { p.addLine(to: c) } else { p.move(to: c); started = true }
                    }
                    return p
                }
                let first = points.firstIndex { $0.account != nil } ?? 0
                let last = points.lastIndex { $0.account != nil } ?? (n - 1)
                let area: Path = {
                    var p = line(\.account)
                    p.addLine(to: CGPoint(x: x(last), y: h))
                    p.addLine(to: CGPoint(x: x(first), y: h))
                    p.closeSubpath()
                    return p
                }()
                let base = y(100)
                ZStack(alignment: .topLeading) {
                    Path { p in p.move(to: CGPoint(x: 0, y: base)); p.addLine(to: CGPoint(x: w, y: base)) }
                        .stroke(hairline, style: StrokeStyle(lineWidth: 1, dash: [2, 3]))
                    area.fill(LinearGradient(colors: [color.opacity(0.20), color.opacity(0)], startPoint: .top, endPoint: .bottom))
                    line(\.spy).stroke(ink3, style: StrokeStyle(lineWidth: 1, dash: [3, 2.5]))
                    line(\.account).stroke(color, style: StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
                    if let v = points[last].account {
                        Circle().fill(color).frame(width: 5, height: 5).position(x: x(last), y: y(v))
                        Circle().stroke(color.opacity(0.35), lineWidth: 4).frame(width: 9, height: 9).position(x: x(last), y: y(v))
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------- 主畫面
struct WidgetView: View {
    @ObservedObject var model: Model
    let openWeb: () -> Void
    let drag: (Bool) -> Bool   // 回傳 true＝這次是點擊（未移動）
    @State private var message: String?

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 24, style: .continuous).fill(Color.black)
            RoundedRectangle(cornerRadius: 24, style: .continuous).strokeBorder(Color.white.opacity(0.10), lineWidth: 1)
            content.padding(.horizontal, 20).padding(.vertical, 18)
        }
        .frame(width: 340, height: 340)
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 0)
            .onChanged { _ in _ = drag(false) }
            .onEnded { _ in if drag(true) { openWeb() } })
        .environment(\.colorScheme, .dark)
    }

    @ViewBuilder var content: some View {
        if let s = model.summary, s.empty {
            VStack(alignment: .leading, spacing: 6) {
                brand(nil)
                Spacer()
                Text("尚無淨值資料").font(display(22, .medium)).foregroundStyle(ink)
                Text("第一次 trader daily 之後出現").font(.system(size: 12)).foregroundStyle(ink2)
                Spacer()
                if s.approvals.n_pending > 0 { approveButton(s.approvals) }
                footnote
            }
        } else if let s = model.summary {
            VStack(alignment: .leading, spacing: 0) {
                brand(s.as_of)
                Text(pct(s.return)).font(display(40, .medium)).foregroundStyle(tone(s.return))
                    .padding(.top, 6).tracking(-0.5)
                HStack(spacing: 12) {
                    Text("SPY \(pct(s.spy_return))").foregroundStyle(ink2)
                    Text("超額 \(pp(s.excess_pp))").foregroundStyle(tone(s.excess_pp))
                }.font(display(12)).padding(.top, 2)
                Spark(points: model.points, color: tone(s.return) == ink ? ink : tone(s.return))
                    .frame(maxHeight: .infinity).padding(.vertical, 12)
                Rectangle().fill(hairline).frame(height: 1)
                HStack(spacing: 0) {
                    stat("收益", usd(s.pnl?.total), tone(s.pnl?.total))
                    stat("現金", pct(s.cash_ratio, 0, sign: false), ink)
                    stat("持股", s.n_positions.map { "\($0) 檔" } ?? "—", ink)
                }.padding(.vertical, 10)
                Rectangle().fill(hairline).frame(height: 1)
                VStack(spacing: 5) {
                    ForEach(s.top3 ?? [], id: \.ticker) { t in
                        HStack {
                            Text(t.ticker).font(.system(size: 12, weight: .semibold)).foregroundStyle(ink).frame(width: 52, alignment: .leading)
                            GeometryReader { g in
                                ZStack(alignment: .leading) {
                                    Capsule().fill(Color.white.opacity(0.06))
                                    Capsule().fill(Color.white.opacity(0.38)).frame(width: g.size.width * min(1, CGFloat((t.weight ?? 0) / 0.15)))
                                }
                            }.frame(height: 3)
                            Text(pct(t.weight, 1, sign: false)).font(display(11.5)).foregroundStyle(ink2).frame(width: 44, alignment: .trailing)
                            Text(pct(t.return, 1)).font(display(11.5)).foregroundStyle(tone(t.return)).frame(width: 52, alignment: .trailing)
                        }
                    }
                    if (s.top3 ?? []).isEmpty { Text("尚無持股").font(.system(size: 12)).foregroundStyle(ink2) }
                }.padding(.top, 10)
                if s.approvals.n_pending > 0 { approveButton(s.approvals).padding(.top, 12) }
                footnote
            }
        } else {
            VStack(alignment: .leading) {
                brand(nil)
                Spacer()
                Text(model.error ?? "載入中…").font(.system(size: 13)).foregroundStyle(ink2)
                Spacer()
            }
        }
    }

    func brand(_ asof: String?) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text("帳戶報酬").font(.system(size: 12, weight: .medium)).foregroundStyle(ink2)
            Spacer()
            if let a = asof { Text("\(String(a.dropFirst(5))) 收盤").font(display(11)).foregroundStyle(ink3) }
        }
    }

    func stat(_ k: String, _ v: String, _ c: Color) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(k).font(.system(size: 10.5)).foregroundStyle(ink3)
            Text(v).font(display(14, .medium)).foregroundStyle(c)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder var footnote: some View {
        if let m = message { Text(m).font(.system(size: 10.5)).foregroundStyle(ink2).padding(.top, 6) }
    }

    func approveButton(_ a: Approvals) -> some View {
        Button {
            let tok = String(format: "%.1fM", Double(a.est_input_tokens) / 1e6)
            let alert = NSAlert()
            alert.messageText = "同意以 opus 分析 \(a.n_pending) 檔（約 \(tok) token）？"
            alert.informativeText = "約 \(Int(a.est_minutes.rounded())) 分鐘。延後的項目不含在內。程式讀不到訂閱額度，請先確認 Claude 額度足夠。"
            alert.addButton(withTitle: "同意並執行")
            alert.addButton(withTitle: "取消")
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() == .alertFirstButtonReturn {
                Task { message = await model.approveAll() }
            }
        } label: {
            HStack(spacing: 8) {
                Text("同意分析").font(.system(size: 12.5, weight: .semibold))
                Text("\(a.n_pending) 檔 · \(String(format: "%.1fM", Double(a.est_input_tokens) / 1e6)) token · \(Int(a.est_minutes.rounded())) 分")
                    .font(display(11.5)).opacity(0.85)
            }
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity).frame(height: 32)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(clay))
        }
        .buttonStyle(.plain)
        .disabled(model.busy)
    }
}

// ---------------------------------------------------------------- 視窗
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let model = Model()
    var startMouse: NSPoint?
    var startOrigin: NSPoint = .zero
    var moved = false
    var timer: Timer?

    func applicationDidFinishLaunching(_ n: Notification) {
        let size = NSSize(width: 340, height: 340)
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let d = UserDefaults.standard
        let origin = d.object(forKey: "originX") != nil
            ? NSPoint(x: d.double(forKey: "originX"), y: d.double(forKey: "originY"))
            : NSPoint(x: screen.maxX - size.width - 24, y: screen.maxY - size.height - 24)
        window = NSWindow(contentRect: NSRect(origin: origin, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        // 桌面圖示層之上、一般視窗之下：看起來像小工具，可點擊
        window.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        let view = WidgetView(model: model, openWeb: { NSWorkspace.shared.open(options.base) },
                              drag: { [weak self] end in self?.drag(end) ?? false })
        window.contentView = NSHostingView(rootView: view)
        window.orderFrontRegardless()
        Task { await model.refresh() }
        // 每 5 秒檢查一次：上次失敗（例如開機時伺服器尚未啟動）立即重試，否則每 60 秒更新
        var last = Date.distantPast
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self else { return }
            Task { @MainActor in
                if self.model.error != nil || Date().timeIntervalSince(last) >= 60 {
                    last = Date()
                    await self.model.refresh()
                }
            }
        }
    }

    /// 以螢幕絕對座標（NSEvent.mouseLocation）計算位移：不受視窗移動影響，拖曳不抽動。
    func drag(_ end: Bool) -> Bool {
        let m = NSEvent.mouseLocation
        if startMouse == nil {
            startMouse = m
            startOrigin = window.frame.origin
            moved = false
        }
        let dx = m.x - startMouse!.x, dy = m.y - startMouse!.y
        if abs(dx) > 3 || abs(dy) > 3 { moved = true }
        if moved {
            window.setFrameOrigin(NSPoint(x: (startOrigin.x + dx).rounded(), y: (startOrigin.y + dy).rounded()))
        }
        guard end else { return false }
        startMouse = nil
        if moved {
            UserDefaults.standard.set(window.frame.origin.x, forKey: "originX")
            UserDefaults.standard.set(window.frame.origin.y, forKey: "originY")
        }
        return !moved
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
}

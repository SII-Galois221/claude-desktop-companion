import AppKit
import Foundation
import Darwin

// The menu is a controller and status viewer, not the network boundary.
// A missing, stale, dry-run, or unacknowledged state must never appear green.
struct GuardSnapshot {
    var installed = false
    var running = false
    var paused = false
    var desiredPaused = false
    var state = "unavailable"
    var mode = "unknown"
    var healthyVerified = false
    var age: Double? = nil
    var desiredGeneration: String? = nil
    var ackGeneration: String? = nil
    var lastProbe: String? = nil
    var detail = "正在读取状态"
    var canStart = true
    var ownerPID: Int32? = nil

    static func generation(_ value: Any?) -> String? {
        guard let value = value, !(value is NSNull) else { return nil }
        if let s = value as? String { return s }
        if let n = value as? NSNumber { return n.stringValue }
        return nil
    }

    init(_ json: [String: Any] = [:]) {
        installed = json["installed"] as? Bool ?? false
        running = json["running"] as? Bool ?? false
        paused = json["paused"] as? Bool ?? false
        desiredPaused = json["desired_paused"] as? Bool ?? paused
        state = json["state"] as? String ?? "unavailable"
        mode = json["monitor_mode"] as? String ?? "unknown"
        healthyVerified = json["healthy_verified"] as? Bool ?? false
        age = (json["status_age_seconds"] as? NSNumber)?.doubleValue
        desiredGeneration = Self.generation(json["control_generation"])
        ackGeneration = Self.generation(json["ack_generation"])
        lastProbe = json["last_probe_at"] as? String
        detail = json["detail"] as? String ?? ""
        canStart = json["can_start"] as? Bool ?? false
        ownerPID = (json["owner_pid"] as? NSNumber)?.int32Value
    }
}

enum Indicator: Equatable { case gray, blue, green, yellow, red }
struct Presentation {
    let title: String
    let indicator: Indicator
}

func menuBarText(_ presentation: Presentation) -> String {
    let compact = [
        "正在处理，请稍候": "处理中",
        "状态不可用": "状态未知",
        "观察连接 · 自动退出未启用": "未启用保护",
        "后台监测未运行": "监测未运行",
        "监测进程归属未确认": "状态待确认",
        "状态已过期 · 无法确认保护": "状态已过期",
        "等待后台确认设置": "确认设置中",
        "仅演练 · 自动退出未启用": "保护未启用",
        "待机 · Claude 未运行": "待机",
        "代理配置已变化": "配置已变化"
    ]
    return compact[presentation.title] ?? presentation.title
}

func present(_ snapshot: GuardSnapshot, elapsed: Double = 0, busy: Bool = false,
             received: Bool = true, expectedOwnerPID: Int32 = ProcessInfo.processInfo.processIdentifier) -> Presentation {
    if busy { return Presentation(title: "正在处理，请稍候", indicator: .yellow) }
    if !received { return Presentation(title: "状态不可用", indicator: .gray) }
    if !snapshot.installed { return Presentation(title: "观察连接 · 自动退出未启用", indicator: .gray) }
    if !snapshot.running { return Presentation(title: "后台监测未运行", indicator: .red) }
    if snapshot.ownerPID != expectedOwnerPID {
        return Presentation(title: "监测进程归属未确认", indicator: .red)
    }
    guard let age = snapshot.age, age.isFinite, age >= 0,
          elapsed.isFinite, age + max(0, elapsed) <= 12 else {
        return Presentation(title: "状态已过期 · 无法确认保护", indicator: .red)
    }
    if snapshot.desiredGeneration == nil || snapshot.ackGeneration == nil ||
        snapshot.desiredGeneration != snapshot.ackGeneration || snapshot.desiredPaused != snapshot.paused {
        return Presentation(title: "等待后台确认设置", indicator: .yellow)
    }
    if snapshot.mode != "apply" {
        return Presentation(title: "仅演练 · 自动退出未启用", indicator: .yellow)
    }
    if snapshot.paused { return Presentation(title: "已暂停", indicator: .yellow) }
    if snapshot.state == "idle" { return Presentation(title: "待机 · Claude 未运行", indicator: .blue) }
    if snapshot.state == "healthy" && snapshot.healthyVerified {
        return Presentation(title: "保护正常", indicator: .green)
    }
    if ["checking", "probe_retry", "starting", "healthy"].contains(snapshot.state) {
        return Presentation(title: "正在检测", indicator: .yellow)
    }
    if snapshot.state == "routing_unverified" {
        return Presentation(title: "出口待核实", indicator: .yellow)
    }
    if snapshot.state == "no_active_flows" {
        return Presentation(title: "暂无活动连接", indicator: .blue)
    }
    if snapshot.state == "routing_warning" {
        return Presentation(title: "发现异常出口", indicator: .red)
    }
    if snapshot.state == "configuration_changed" {
        return Presentation(title: "代理配置已变化", indicator: .red)
    }
    return Presentation(title: "保护异常", indicator: .red)
}

final class Backend {
    func call(_ command: String, completion: @escaping ([String: Any]) -> Void) {
        DispatchQueue.global(qos: .utility).async {
            guard let script = Bundle.main.url(forResource: "controller", withExtension: "py") else {
                DispatchQueue.main.async { completion(["ok": false, "detail": "应用文件不完整：缺少控制程序。请重新构建应用。"] ) }
                return
            }
            let process = Process()
            guard let pythonURL = PythonRuntime.executable else {
                DispatchQueue.main.async { completion(["ok": false, "detail": "未找到 Python 3.11+。请先安装 Python（参见 README），然后重新打开应用。"]) }
                return
            }
            process.executableURL = pythonURL
            process.arguments = ["-B", script.path, command]
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            // Connections can exceed a pipe buffer. Capture into a private file
            // so waitUntilExit cannot deadlock on a full stdout pipe.
            var template = Array(FileManager.default.temporaryDirectory.appendingPathComponent("claude-guard-ui-XXXXXX").path.utf8CString)
            let fd = mkstemp(&template)
            guard fd >= 0 else {
                DispatchQueue.main.async { completion(["ok": false, "detail": "无法创建私有结果文件。"] ) }
                return
            }
            let outputPath = String(cString: template)
            let output = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            defer { try? output.close(); try? FileManager.default.removeItem(atPath: outputPath) }
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                let timeout: TimeInterval = command == "start" ? 120 : (["check", "flows", "stop"].contains(command) ? 30 : 12)
                let deadline = Date().addingTimeInterval(timeout)
                while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
                if process.isRunning {
                    process.terminate() // Only this UI-owned controller invocation; never Claude.
                    DispatchQueue.main.async { completion(["ok": false, "detail": "控制程序响应超时。请查看日志；当前状态尚未确认。"] ) }
                    return
                }
                process.waitUntilExit()
                try output.seek(toOffset: 0)
                let data = try output.read(upToCount: 1_048_577) ?? Data()
                guard data.count <= 1_048_576 else {
                    DispatchQueue.main.async { completion(["ok": false, "detail": "连接结果超过 1 MiB，无法完整显示；请查看日志。此次结果没有被当作正常状态。"] ) }
                    return
                }
                guard
                      let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw NSError(domain: "InvalidStatus", code: 1)
                }
                DispatchQueue.main.async { completion(json) }
            } catch {
                DispatchQueue.main.async { completion(["ok": false, "detail": "无法读取控制程序结果。请查看应用说明或日志。"] ) }
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private let backend = Backend()
    private let historyController = HistoryWindowController()
    private var statusItem: NSStatusItem!
    private var menu = NSMenu()
    private var timer: Timer?
    private var snapshot = GuardSnapshot()
    private var receivedAt: Date?
    private var pollPending = false
    private var busy = false
    private var revision = 0
    private var transientDetail: String?
    private var stateItem: NSMenuItem!
    private var pauseItem: NSMenuItem!
    private var quitItem: NSMenuItem!
    private var statusWindow: NSWindow?
    private var windowStateLabel: NSTextField?
    private var windowPauseButton: NSButton?
    private var windowQuitButton: NSButton?
    private var quitPending = false
    private var quitApproved = false
    private var flowsWindow: NSWindow?
    private var flowsTable: NSTableView?
    private var flowRows: [[String: Any]] = []
    private var flowsPending = false
    private var flowsTimer: Timer?
    private var flowsReceivedAt: Date?
    private var flowsSummaryLabel: NSTextField?
    private var flowsEgressLabel: NSTextField?
    private var flowsCoverageLabel: NSTextField?
    private var flowsTimeLabel: NSTextField?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        historyController.guardAllowsOperation = { [weak self] in
            guard let self = self else { return false }
            return !self.busy && !self.quitPending
        }
        historyController.activityChanged = { [weak self] in self?.render() }
        // LSUIElement apps still need a main menu to route standard Cmd-Q.
        // Using NSApp.terminate preserves applicationShouldTerminate's stop ACK.
        let mainMenu = NSMenu()
        let appMenuItem = NSMenuItem(title: "Claude Desktop Companion", action: nil, keyEquivalent: "")
        let appMenu = NSMenu(title: "Claude Desktop Companion")
        let historyCommand = NSMenuItem(title: "备份与导入", action: #selector(showHistoryWindow), keyEquivalent: "1")
        historyCommand.keyEquivalentModifierMask = [.command]
        historyCommand.target = self
        appMenu.addItem(historyCommand)
        appMenu.addItem(.separator())
        let quitCommand = NSMenuItem(title: "退出并停止监测", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quitCommand.keyEquivalentModifierMask = [.command]
        quitCommand.target = NSApp
        appMenu.addItem(quitCommand); appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem); NSApp.mainMenu = mainMenu
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "读取状态中"
        statusItem.button?.imagePosition = .imageLeading
        menu.autoenablesItems = false
        menu.delegate = self
        stateItem = add("正在读取状态", #selector(showFlowsWindow))
        add("备份与导入", #selector(showHistoryWindow))
        menu.addItem(.separator())
        pauseItem = add("暂停", #selector(togglePause))
        quitItem = add("退出", #selector(quitMenu))
        statusItem.menu = menu
        render()
        poll()
        timer = Timer(timeInterval: 2, repeats: true) { [weak self] _ in
            self?.render(); self?.poll()
        }
        // Keep stale-state detection alive while the status menu is open.
        if let timer = timer { RunLoop.main.add(timer, forMode: .common) }
        let flowTimer = Timer(timeInterval: 5, repeats: true) { [weak self] _ in
            self?.renderFlowsAge(); self?.refreshFlows()
        }
        flowsTimer = flowTimer; RunLoop.main.add(flowTimer, forMode: .common)
        refreshFlows()
        if !UserDefaults.standard.bool(forKey: "hasPresentedStatusWelcome") {
            UserDefaults.standard.set(true, forKey: "hasPresentedStatusWelcome")
        }
        showStatusWindow()
        // Opening this app starts supervision. First installation still asks
        // locally before interrupting any running Claude work.
        startProtection()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showStatusWindow()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitApproved { return .terminateNow }
        if quitPending { return .terminateLater }
        if historyController.isMutationInProgress {
            alert("请等待历史操作完成", historyController.exitBlockedReason)
            return .terminateCancel
        }
        if busy {
            alert("请等待当前操作完成", "正在执行设置或检查。完成后再退出，以便确认后台监测已经停止。")
            return .terminateCancel
        }
        quitPending = true
        busy = true; revision += 1
        transientDetail = "正在停止后台监测…"; render()
        backend.call("stop") { [weak self] result in
            guard let self = self else { return }
            if result["ok"] as? Bool == true {
                self.quitApproved = true
                NSApp.reply(toApplicationShouldTerminate: true)
            } else {
                self.quitPending = false; self.busy = false; self.receivedAt = nil
                NSApp.reply(toApplicationShouldTerminate: false)
                self.alert("尚不能确认监测已停止", result["detail"] as? String ?? "请查看日志并重试退出。应用暂时保持打开。")
                self.poll(); self.render()
            }
        }
        return .terminateLater
    }

    @objc private func showHistoryWindow() { historyController.show() }

    @objc private func showStatusWindow() {
        if statusWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 145),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Claude Desktop Companion"
            window.isReleasedWhenClosed = false
            let stack = NSStackView()
            stack.orientation = .vertical; stack.alignment = .centerX; stack.spacing = 20
            stack.translatesAutoresizingMaskIntoConstraints = false
            let status = NSTextField(labelWithString: "正在读取状态")
            status.font = .systemFont(ofSize: 20, weight: .medium)
            stack.addArrangedSubview(status); windowStateLabel = status
            let pause = NSButton(title: "暂停", target: self, action: #selector(togglePause))
            let quit = NSButton(title: "退出", target: self, action: #selector(quitMenu))
            pause.bezelStyle = .rounded; quit.bezelStyle = .rounded
            pause.toolTip = "暂停自动退出和外网探测；固定代理设置仍保留"
            quit.toolTip = "退出应用并停止后台监测"
            let buttons = NSStackView(views: [pause, quit]); buttons.spacing = 16
            stack.addArrangedSubview(buttons)
            windowPauseButton = pause; windowQuitButton = quit
            let content = window.contentView!
            content.addSubview(stack)
            NSLayoutConstraint.activate([
                stack.centerXAnchor.constraint(equalTo: content.centerXAnchor),
                stack.centerYAnchor.constraint(equalTo: content.centerYAnchor),
                stack.leadingAnchor.constraint(greaterThanOrEqualTo: content.leadingAnchor, constant: 16),
                stack.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -16)
            ])
            window.center(); statusWindow = window
        }
        render(); poll()
        NSApp.activate(ignoringOtherApps: true)
        statusWindow?.makeKeyAndOrderFront(nil)
    }

    @objc private func showFlowsWindow() {
        if flowsWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1080, height: 600),
                                  styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "Claude 实时连接"
            window.minSize = NSSize(width: 840, height: 430)
            window.isReleasedWhenClosed = false
            let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
            stack.translatesAutoresizingMaskIntoConstraints = false
            let heading = NSTextField(labelWithString: "Claude 实时连接")
            heading.font = .systemFont(ofSize: 20, weight: .semibold); stack.addArrangedSubview(heading)
            let summary = NSTextField(labelWithString: "正在读取当前连接…")
            summary.font = .systemFont(ofSize: 13, weight: .medium)
            stack.addArrangedSubview(summary); flowsSummaryLabel = summary
            let egress = NSTextField(wrappingLabelWithString: "代理出口抽测：尚未核验（不能代表所有连接）")
            egress.font = .systemFont(ofSize: 12)
            stack.addArrangedSubview(egress); flowsEgressLabel = egress
            let coverage = NSTextField(wrappingLabelWithString: "仅显示当前可观测到的连接。未观测到连接不代表没有流量，也不能据此保证所有流量的出口。")
            coverage.font = .systemFont(ofSize: 12); coverage.textColor = .secondaryLabelColor
            stack.addArrangedSubview(coverage); flowsCoverageLabel = coverage
            let time = NSTextField(labelWithString: "尚无采样结果")
            time.font = .systemFont(ofSize: 11); time.textColor = .secondaryLabelColor
            let refresh = NSButton(title: "立即刷新", target: self, action: #selector(refreshFlows))
            refresh.bezelStyle = .rounded
            let row = NSStackView(views: [refresh, time]); row.spacing = 14
            stack.addArrangedSubview(row); flowsTimeLabel = time
            let table = NSTableView(); table.delegate = self; table.dataSource = self
            table.usesAlternatingRowBackgroundColors = true; table.rowHeight = 27
            table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
            for (key, title, width) in [("process", "进程", 150.0), ("target", "目标", 235.0),
                                         ("transport", "协议", 65.0), ("chain", "代理链 / 路径", 310.0),
                                         ("verdict", "判定", 140.0)] {
                let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(key))
                column.title = title; column.width = width; column.minWidth = 55
                table.addTableColumn(column)
            }
            let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
            scroll.borderType = .bezelBorder; scroll.documentView = table
            stack.addArrangedSubview(scroll); flowsTable = table
            let content = window.contentView!; content.addSubview(stack)
            NSLayoutConstraint.activate([
                stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
                stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -22),
                stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
                stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20),
                egress.widthAnchor.constraint(equalTo: stack.widthAnchor),
                coverage.widthAnchor.constraint(equalTo: stack.widthAnchor),
                scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
                scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 160)
            ])
            window.center(); flowsWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        flowsWindow?.makeKeyAndOrderFront(nil)
        refreshFlows()
    }

    private func text(_ value: Any?) -> String {
        guard let value = value, !(value is NSNull) else { return "" }
        if let text = value as? String { return text }
        if let number = value as? NSNumber { return number.stringValue }
        return ""
    }

    private func parseTimestamp(_ value: Any?) -> Date? {
        let value = text(value)
        let formatter = ISO8601DateFormatter()
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: value)
    }

    @objc private func refreshFlows() {
        guard !flowsPending && !quitPending else { return }
        flowsPending = true
        backend.call("flows") { [weak self] result in
            guard let self = self else { return }
            self.flowsPending = false
            guard let flows = result["flows"] as? [[String: Any]] else {
                self.flowsReceivedAt = nil
                self.flowsSummaryLabel?.stringValue = "连接状态不可用 · 无法确认出口"
                self.flowsSummaryLabel?.textColor = .systemRed
                self.flowsTimeLabel?.stringValue = "读取失败，请稍后重试"
                self.flowRows = []; self.flowsTable?.reloadData()
                self.flowsEgressLabel?.stringValue = "代理出口抽测：当前无法确认（不能代表所有连接）"
                return
            }
            self.flowRows = flows; self.flowsReceivedAt = self.parseTimestamp(result["sampled_at"])
            let direct = flows.filter { self.text($0["verdict"]) == "direct" }.count
            let house = flows.filter { self.text($0["verdict"]) == "residential" }.count
            let other = flows.filter { self.text($0["verdict"]) == "other_proxy" }.count
            let unknown = flows.filter { self.text($0["verdict"]) == "unverified" }.count
            self.flowsSummaryLabel?.stringValue = "当前可观测 \(flows.count) 条 · 家宽链 \(house) · 其他代理 \(other) · 直连 \(direct) · 未核实 \(unknown)"
            self.flowsSummaryLabel?.textColor = direct > 0 ? .systemRed : (other > 0 || unknown > 0 ? .systemOrange : .labelColor)
            if result["controller_available"] as? Bool != true {
                self.flowsSummaryLabel?.stringValue = "Clash 控制器不可用 · 当前 \(flows.count) 条可观测连接的完整路径无法确认"
                self.flowsSummaryLabel?.textColor = .systemRed
            }
            if result["partial"] as? Bool == true || result["truncated"] as? Bool == true || result["socket_inspection_available"] as? Bool != true {
                self.flowsSummaryLabel?.stringValue += result["truncated"] as? Bool == true ? " · 采样已截断" : " · 采样不完整"
                self.flowsSummaryLabel?.textColor = .systemRed
            }
            self.flowsEgressLabel?.textColor = .labelColor
            if let egress = result["egress"] as? [String: Any], self.text(egress["status"]) == "mismatch" {
                self.flowsEgressLabel?.stringValue = "出口与预期家宽不一致：\(self.text(egress["ip"])) · \(self.text(egress["country"]))（代理入口抽测，不能代表所有连接）"
                self.flowsEgressLabel?.textColor = .systemRed
            } else if let egress = result["egress"] as? [String: Any], self.text(egress["status"]) == "verified",
               let checked = self.parseTimestamp(egress["checked_at"]),
               Date().timeIntervalSince(checked) >= -2, Date().timeIntervalSince(checked) <= 120 {
                let ip = self.text(egress["ip"]), country = self.text(egress["country"])
                let formatter = DateFormatter(); formatter.dateFormat = "HH:mm:ss"
                self.flowsEgressLabel?.stringValue = "代理出口抽测：\(ip.isEmpty ? "未知 IP" : ip) · \(country.isEmpty ? "地区未提供" : country) · \(formatter.string(from: checked))（不能代表所有连接）"
            } else {
                self.flowsEgressLabel?.stringValue = "代理出口抽测：尚未核验 / 当前无法确认（不能代表所有连接）"
            }
            let note = self.text(result["coverage_note"])
            self.flowsCoverageLabel?.stringValue = note.isEmpty ? "仅显示当前可观测连接。未观测到连接不代表没有流量，也不能据此保证所有流量的出口。" : note
            if let scope = result["scope"] as? [String: Any], let excluded = scope["excluded_process_count"] as? Int, excluded > 0 {
                self.flowsCoverageLabel?.stringValue += " 另有 \(excluded) 个独立 / 归属未确认的 Claude 进程未纳入。"
            }
            self.flowsTable?.reloadData(); self.renderFlowsAge()
            self.poll() // status may now carry a routing warning from this sample.
        }
    }

    private func renderFlowsAge() {
        guard let received = flowsReceivedAt, Date().timeIntervalSince(received) >= -2 else {
            flowsTimeLabel?.stringValue = "采样时间无法确认"
            flowsTimeLabel?.textColor = .systemRed
            return
        }
        let age = max(0, Int(Date().timeIntervalSince(received)))
        flowsTimeLabel?.stringValue = age > 15 ? "列表已过期（\(age) 秒前），当前出口无法确认" : "\(age) 秒前更新 · 应用运行时每 5 秒刷新"
        flowsTimeLabel?.textColor = age > 15 ? .systemRed : .secondaryLabelColor
        if age > 15 {
            flowsSummaryLabel?.stringValue = "连接列表已过期 · 当前出口无法确认"
            flowsSummaryLabel?.textColor = .systemRed
        }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { flowRows.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row >= 0 && row < flowRows.count, let column = tableColumn else { return nil }
        let data = flowRows[row], key = column.identifier.rawValue
        let label = NSTextField(labelWithString: ""); label.lineBreakMode = .byTruncatingMiddle
        label.font = .systemFont(ofSize: 12)
        switch key {
        case "chain":
            let chain = data["chain"] as? [String] ?? []
            label.stringValue = chain.isEmpty ? text(data["via"]) : chain.joined(separator: " → ")
        case "verdict":
            switch text(data["verdict"]) {
            case "residential": label.stringValue = "已见家宽链"; label.textColor = .systemGreen
            case "other_proxy": label.stringValue = "其他代理"; label.textColor = .systemOrange
            case "direct": label.stringValue = "直连"; label.textColor = .systemRed
            case "local": label.stringValue = "本机连接"; label.textColor = .secondaryLabelColor
            default: label.stringValue = "未核实"; label.textColor = .systemOrange
            }
        case "process":
            label.stringValue = text(data["process"])
            let pid = text(data["pid"]); if !pid.isEmpty { label.stringValue += " (\(pid))" }
        default: label.stringValue = text(data[key])
        }
        label.toolTip = label.stringValue + (text(data["detail"]).isEmpty ? "" : "\n" + text(data["detail"]))
        return label
    }

    @discardableResult private func add(_ title: String, _ action: Selector?) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self; menu.addItem(item); return item
    }

    func menuWillOpen(_ menu: NSMenu) { render(); poll() }

    private func poll() {
        guard !pollPending else { return }
        pollPending = true
        let requestedRevision = revision
        backend.call("status") { [weak self] result in
            guard let self = self else { return }
            self.pollPending = false
            if requestedRevision != self.revision { self.poll(); return }
            if result["installed"] is Bool && result["running"] is Bool {
                self.snapshot = GuardSnapshot(result)
                self.receivedAt = Date()
                if !self.busy { self.transientDetail = nil }
            } else {
                self.receivedAt = nil
                self.transientDetail = result["detail"] as? String ?? "状态不可用"
            }
            self.render()
        }
    }

    private func render() {
        guard statusItem != nil else { return }
        let p = present(snapshot, elapsed: receivedAt.map { Date().timeIntervalSince($0) } ?? 0,
                        busy: busy, received: receivedAt != nil)
        stateItem.title = p.title
        let detail = transientDetail ?? snapshot.detail
        stateItem.toolTip = detail + "\n点击查看实时连接"
        let color: NSColor
        switch p.indicator {
        case .green: color = .systemGreen
        case .yellow: color = .systemOrange
        case .red: color = .systemRed
        case .blue: color = .systemBlue
        case .gray: color = .secondaryLabelColor
        }
        let symbol = p.indicator == .green ? "checkmark.shield.fill" :
            (p.indicator == .red ? "exclamationmark.shield.fill" : "shield.lefthalf.filled")
        statusItem.button?.title = menuBarText(p)
        statusItem.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: p.title)
        statusItem.button?.contentTintColor = color
        statusItem.button?.toolTip = "Claude Desktop Companion：" + p.title
        statusItem.button?.setAccessibilityLabel("Claude Desktop Companion：" + p.title)
        pauseItem.title = !snapshot.running || snapshot.desiredPaused ? "恢复" : "暂停"
        pauseItem.isEnabled = !busy && receivedAt != nil && !historyController.isMutationInProgress
        pauseItem.toolTip = "暂停自动退出和外网探测；固定代理设置仍保留"
        quitItem.isEnabled = !busy
        quitItem.toolTip = "退出应用并停止后台监测"
        windowStateLabel?.stringValue = p.title
        windowStateLabel?.textColor = color
        windowStateLabel?.toolTip = detail
        windowPauseButton?.title = pauseItem.title
        windowPauseButton?.isEnabled = pauseItem.isEnabled
        windowQuitButton?.isEnabled = !busy
        historyController.updateGuardAvailability()
    }

    private func alert(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert(); a.messageText = title; a.informativeText = text
        a.addButton(withTitle: "知道了"); a.runModal()
    }

    private func perform(_ command: String, done: (([String: Any]) -> Void)? = nil) {
        guard !busy else { return }
        revision += 1
        busy = true; transientDetail = nil; render()
        backend.call(command) { [weak self] result in
            guard let self = self else { return }
            self.revision += 1
            self.receivedAt = nil // Never repaint an old green after a command.
            self.busy = false
            if let done = done { done(result) }
            else if result["ok"] as? Bool != true {
                self.alert("操作尚未完成", result["detail"] as? String ?? "请查看日志。")
            }
            self.poll(); self.render()
        }
    }

    @objc private func startProtection() {
        guard !historyController.isMutationInProgress else { return }
        perform("start") { [weak self] result in
            guard let self = self else { return }
            if result["requires_restart"] as? Bool == true { self.confirmRestart() }
            else if result["ok"] as? Bool != true {
                self.alert("尚未启用保护", result["detail"] as? String ?? "请查看日志。")
            }
        }
    }

    private func confirmRestart() {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "启用保护需要重启 Claude"
        a.informativeText = "将正常退出并重新打开 Claude，当前后台任务可能中断。聊天记录和登录配置不会被删除。"
        a.addButton(withTitle: "取消"); a.addButton(withTitle: "重启并启用")
        guard a.runModal() == .alertSecondButtonReturn else { return }
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.anthropic.claudefordesktop")
            .filter { $0.bundleURL?.standardizedFileURL.path == "/Applications/Claude.app" }
        let reopen = !apps.isEmpty
        busy = true; transientDetail = "等待 Claude 正常退出…"; render()
        apps.forEach { _ = $0.terminate() }
        waitForExit(apps, deadline: Date().addingTimeInterval(15)) { [weak self] closed in
            guard let self = self else { return }
            self.busy = false
            if !closed {
                self.alert("Claude 尚未退出", "请在 Claude 中结束或保存当前任务，再正常退出应用，然后重新点击“启用保护”。不会强制结束进程。")
                self.poll(); self.render(); return
            }
            self.perform("start") { [weak self] result in
                guard let self = self else { return }
                if result["ok"] as? Bool == true {
                    if reopen {
                        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/Claude.app"),
                            configuration: NSWorkspace.OpenConfiguration()) { _, error in
                            if error != nil { DispatchQueue.main.async { self.alert("保护已启用", "Claude 未能自动重新打开，请手动打开应用。") } }
                        }
                    }
                } else {
                    self.alert("尚未启用保护", result["detail"] as? String ?? "配置未完成，请查看日志。Claude 保持关闭。")
                }
            }
        }
    }

    private func waitForExit(_ apps: [NSRunningApplication], deadline: Date,
                             done: @escaping (Bool) -> Void) {
        if apps.allSatisfy({ $0.isTerminated }) { done(true); return }
        if Date() >= deadline { done(false); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            self?.waitForExit(apps, deadline: deadline, done: done)
        }
    }

    @objc private func togglePause() {
        guard !historyController.isMutationInProgress else { return }
        if !snapshot.installed || !snapshot.running { startProtection() }
        else { perform(snapshot.desiredPaused ? "resume" : "pause") }
    }
    @objc private func checkNow() {
        perform("check") { [weak self] result in
            self?.alert(result["ok"] as? Bool == true ? "检查完成" : "检查未通过",
                        result["detail"] as? String ?? "请查看菜单中的后台实时状态。")
        }
    }
    @objc private func openLogs() { perform("open_logs") }
    @objc private func openReadme() {
        if let url = Bundle.main.url(forResource: "README", withExtension: "md") { NSWorkspace.shared.open(url) }
        else { alert("应用说明不可用", "请重新构建应用或检查 Resources/README.md。") }
    }
    @objc private func quitMenu() { NSApp.terminate(nil) }
}

func runSelfTests() {
    let base: [String: Any] = ["installed": true, "running": true, "paused": false,
        "desired_paused": false, "state": "healthy", "monitor_mode": "apply",
        "healthy_verified": true, "status_age_seconds": 1,
        "control_generation": "g1", "ack_generation": "g1",
        "owner_pid": ProcessInfo.processInfo.processIdentifier]
    var count = 0
    func expect(_ changes: [String: Any] = [:], _ indicator: Indicator, elapsed: Double = 0,
                busy: Bool = false, received: Bool = true) {
        let json = base.merging(changes) { _, replacement in replacement }
        precondition(present(GuardSnapshot(json), elapsed: elapsed, busy: busy, received: received).indicator == indicator)
        count += 1
    }
    expect([:], .green)
    expect(["installed": false], .gray)
    expect(["running": false], .red)
    expect(["owner_pid": -1], .red)
    expect(["owner_pid": NSNull()], .red)
    expect(["monitor_mode": "dry-run"], .yellow)
    expect(["paused": true, "desired_paused": true], .yellow)
    expect(["desired_paused": true], .yellow)
    expect(["ack_generation": "old"], .yellow)
    expect(["ack_generation": NSNull()], .yellow)
    expect(["healthy_verified": false], .yellow)
    expect(["status_age_seconds": 13], .red)
    expect(["status_age_seconds": -1], .red)
    expect(["status_age_seconds": NSNull()], .red)
    expect([:], .red, elapsed: 12)
    expect([:], .yellow, busy: true)
    expect([:], .gray, received: false)
    expect(["state": "idle"], .blue)
    expect(["state": "egress_unavailable"], .red)
    expect(["state": "unrecognized"], .red)
    print("menu_state_tests_passed=\(count)")
}

@main
struct ClaudeResidentialGuardMain {
    static func main() {
        if CommandLine.arguments.contains("--self-test") {
            runSelfTests()
            runHistorySelfTests()
        } else {
            let application = NSApplication.shared
            let delegate = AppDelegate()
            application.delegate = delegate
            application.run()
        }
    }
}

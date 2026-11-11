import AppKit
import Foundation
import Darwin

private func validHistoryAlias(_ value: String) -> Bool {
    value.range(of: "^[a-z][a-z0-9_-]{0,31}\\z", options: .regularExpression) != nil
}

func historyIdentityMatches(_ result: [String: Any], target: String, version: String) -> Bool {
    result["ok"] as? Bool == true && result["active_alias"] as? String == target &&
        result["version"] as? String == version && result["supported_version"] as? Bool == true
}

struct HistoryPreview {
    let id: String
    let backupID: String
    let backupCreatedAt: String
    let source: String
    let target: String
    let missing: Int
    let continuation: Int
    let unchanged: Int
    let equivalent: Int
    let skipped: Int
    let conflicts: Int
    let restoreFiles: Int
    let retainedNewer: Int
    let allowed: Bool
    let detail: String
    let items: [[String: Any]]

    init?(_ json: [String: Any]) {
        guard json["ok"] as? Bool == true,
              let id = json["preview_id"] as? String, !id.isEmpty,
              let backupID = json["backup_id"] as? String, !backupID.isEmpty,
              let source = json["source"] as? String, validHistoryAlias(source),
              let target = json["target"] as? String, validHistoryAlias(target),
              let missing = json["missing_count"] as? Int, missing >= 0,
              let continuation = json["continuation_count"] as? Int, continuation >= 0,
              let unchanged = json["unchanged_count"] as? Int, unchanged >= 0,
              let equivalent = json["equivalent_count"] as? Int, equivalent >= 0,
              let skipped = json["skipped_count"] as? Int, skipped >= 0,
              let conflicts = json["conflict_count"] as? Int, conflicts >= 0 else { return nil }
        let restoreFiles = json["restore_file_count"] as? Int ?? 0
        let retainedNewer = json["retained_newer_count"] as? Int ?? 0
        guard restoreFiles >= 0, retainedNewer >= 0 else { return nil }
        self.id = id; self.backupID = backupID
        self.backupCreatedAt = json["backup_created_at"] as? String ?? ""
        self.source = source; self.target = target
        self.missing = missing; self.continuation = continuation
        self.unchanged = unchanged; self.equivalent = equivalent
        self.skipped = skipped; self.conflicts = conflicts
        self.restoreFiles = restoreFiles; self.retainedNewer = retainedNewer
        self.allowed = json["can_sync"] as? Bool == true
        self.detail = json["detail"] as? String ?? ""
        self.items = json["items"] as? [[String: Any]] ?? []
    }

    func canApply(backupID selected: String?, currentAccount: String?, supported: Bool) -> Bool {
        supported && allowed && conflicts == 0 && (missing > 0 || continuation > 0 || restoreFiles > 0) &&
            backupID == selected && target == currentAccount
    }
}

struct SavedHistoryBackup: Equatable {
    let id: String
    let source: String
    let createdAt: String
    let records: Int
    let path: String

    init?(_ json: [String: Any]) {
        guard let id = json["backup_id"] as? String, !id.isEmpty,
              let source = json["source"] as? String, validHistoryAlias(source),
              let createdAt = json["created_at"] as? String, !createdAt.isEmpty,
              let records = json["records"] as? Int, records >= 0,
              let path = json["path"] as? String, path.hasPrefix("/") else { return nil }
        self.id = id; self.source = source; self.createdAt = createdAt
        self.records = records; self.path = path
    }
}

// History commands use their own worker, so large backups never delay guard polls.
// stdout goes to a mode-0600 file: no full-pipe waitUntilExit deadlock.
final class HistoryBackend {
    func call(_ command: String, request: [String: Any] = [:],
              delayed: @escaping () -> Void,
              completion: @escaping ([String: Any]) -> Void) {
        DispatchQueue.global(qos: .utility).async {
            func finish(_ json: [String: Any]) {
                DispatchQueue.main.async { completion(json) }
            }
            guard let script = Bundle.main.url(forResource: "history_service", withExtension: "py") else {
                finish(["ok": false, "detail": "应用缺少历史管理程序，请重新安装完整应用。"]); return
            }
            let process = Process()
            guard let pythonURL = PythonRuntime.executable else {
                finish(["ok": false, "detail": "未找到 Python 3.11+。请先安装 Python（参见 README），然后重新打开应用。"])
                return
            }
            process.executableURL = pythonURL
            process.arguments = ["-B", script.path, command]
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            var template = Array(FileManager.default.temporaryDirectory.appendingPathComponent("claude-history-ui-XXXXXX").path.utf8CString)
            let fd = mkstemp(&template)
            guard fd >= 0 else {
                finish(["ok": false, "detail": "无法创建私有结果文件，请检查磁盘空间后重试。"]); return
            }
            _ = fchmod(fd, S_IRUSR | S_IWUSR)
            let outputPath = String(cString: template)
            let output = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            defer { try? output.close(); try? FileManager.default.removeItem(atPath: outputPath) }
            let input = Pipe()
            process.standardInput = input
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            let mutation = ["backup", "sync"].contains(command)
            do {
                let data = try JSONSerialization.data(withJSONObject: request)
                try process.run()
                try input.fileHandleForWriting.write(contentsOf: data)
                try input.fileHandleForWriting.close()
                let deadline = Date().addingTimeInterval(mutation ? 60 : 45)
                var reportedDelay = false
                while process.isRunning {
                    if Date() >= deadline && !reportedDelay {
                        reportedDelay = true
                        if mutation {
                            // A copying transaction may take minutes. Never interrupt it
                            // at a UI timeout or release the UI mutation lock early.
                            DispatchQueue.main.async { delayed() }
                        } else {
                            process.terminate()
                            finish(["ok": false, "detail": "读取超时，当前账号和记录状态尚未确认。请稍后刷新。"])
                            return
                        }
                    }
                    Thread.sleep(forTimeInterval: 0.08)
                }
                process.waitUntilExit()
                try output.seek(toOffset: 0)
                let resultData = try output.read(upToCount: 8_388_609) ?? Data()
                guard resultData.count <= 8_388_608 else {
                    finish(["ok": false, "detail": "结果过大，无法完整显示。操作状态尚未确认，请刷新后检查记录。"]); return
                }
                guard var json = try JSONSerialization.jsonObject(with: resultData) as? [String: Any] else {
                    throw NSError(domain: "HistoryResult", code: 1)
                }
                if process.terminationStatus != 0 && json["ok"] as? Bool == true {
                    json = ["ok": false, "detail": "历史管理程序异常结束，操作状态尚未确认。请刷新后检查记录。"]
                }
                finish(json)
            } catch {
                // Even an input/output failure must not abandon a running mutation.
                try? input.fileHandleForWriting.close()
                if process.isRunning && mutation {
                    DispatchQueue.main.async { delayed() }
                    process.waitUntilExit()
                } else if process.isRunning { process.terminate() }
                finish(["ok": false, "detail": "无法启动或读取历史管理程序：\(error.localizedDescription)。请刷新后检查记录，勿重复执行尚未确认的操作。"])
            }
        }
    }
}

final class HistoryWindowController: NSObject, NSWindowDelegate, NSMenuDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private enum Activity { case idle, status, backup, preview, confirming, quitting, sync, reopening }
    private let backend = HistoryBackend()
    private var window: NSWindow?
    private var timer: Timer?
    private var activity: Activity = .idle
    private var status: [String: Any]?
    private var statusReceived: Date?
    private var preview: HistoryPreview?
    private var previewVersion: String?
    private var message = "备份当前账号，或将保存的备份导入当前账号。这里处理本地 Code 记录。"
    private var messageIsError = false
    private var statusErrorVisible = false
    private var currentLabel: NSTextField!
    private var accountsSummary: NSTextField!
    private var backupButton: NSButton!
    private var revealBackupButton: NSButton!
    private var lastBackupPath: String?
    private var preferredBackupPath: String?
    private var savedBackups: [SavedHistoryBackup] = []
    private var backupMenuOpen = false
    private var refreshButton: NSButton!
    private var backupPopup: NSPopUpButton!
    private var targetLabel: NSTextField!
    private var previewButton: NSButton!
    private var syncButton: NSButton!
    private var summaryLabel: NSTextField!
    private var messageLabel: NSTextField!
    private var progress: NSProgressIndicator!
    private var table: NSTableView!

    // Called only on the main thread. The app owns the guard lifecycle separately.
    var guardAllowsOperation: () -> Bool = { true }
    var activityChanged: () -> Void = {}
    var isMutationInProgress: Bool {
        [.backup, .confirming, .quitting, .sync, .reopening].contains(activity)
    }
    var exitBlockedReason: String {
        activity == .confirming ? "请先完成或取消导入确认，再退出本应用。" :
            "正在备份或导入本地记录。请等待结果后再退出，以免中断写入；网络监测继续独立运行。"
    }

    private var activeAlias: String? {
        guard let alias = status?["active_alias"] as? String, validHistoryAlias(alias) else { return nil }
        return alias
    }
    private var selectedBackupID: String? { backupPopup?.selectedItem?.representedObject as? String }
    private var selectedBackup: SavedHistoryBackup? { savedBackups.first { $0.id == selectedBackupID } }
    private var statusIsFresh: Bool {
        guard let received = statusReceived else { return false }
        return Date().timeIntervalSince(received) >= 0 && Date().timeIntervalSince(received) <= 30
    }
    private var supported: Bool { status?["supported_version"] as? Bool == true }
    private var canOperate: Bool { activity == .idle && guardAllowsOperation() }
    private var backupPath: String? {
        if let path = selectedBackup?.path { return path }
        let accounts = status?["accounts"] as? [[String: Any]] ?? []
        return accounts.first { $0["alias"] as? String == activeAlias }?["latest_backup"] as? String ?? lastBackupPath
    }

    func show() {
        if window == nil { buildWindow() }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        render(); refreshStatus()
        if timer == nil {
            let timer = Timer(timeInterval: 10, repeats: true) { [weak self] _ in
                guard let self = self, self.window?.isVisible == true else { return }
                self.render(); self.refreshStatus()
            }
            self.timer = timer; RunLoop.main.add(timer, forMode: .common)
        }
    }

    func updateGuardAvailability() { if window != nil { render() } }

    private func label(_ value: String, size: CGFloat = 12, weight: NSFont.Weight = .regular,
                       secondary: Bool = false) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: value)
        label.font = .systemFont(ofSize: size, weight: weight)
        label.textColor = secondary ? .secondaryLabelColor : .labelColor
        label.setContentCompressionResistancePriority(.required, for: .vertical)
        return label
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded; return button
    }

    private func buildWindow() {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = "备份与导入 · Claude Desktop Companion"
        window.minSize = NSSize(width: 760, height: 560)
        window.isReleasedWhenClosed = false; window.delegate = self
        let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 7
        stack.translatesAutoresizingMaskIntoConstraints = false
        let heading = label("备份与导入", size: 20, weight: .semibold)
        stack.addArrangedSubview(heading)
        currentLabel = label("正在读取当前账号与 Claude 版本…", size: 12)
        stack.addArrangedSubview(currentLabel)

        accountsSummary = label("账号信息将在登录并登记后显示。", size: 11, secondary: true)
        accountsSummary.maximumNumberOfLines = 2
        stack.addArrangedSubview(accountsSummary)
        backupButton = button("备份当前账号", #selector(backupCurrent))
        refreshButton = button("刷新", #selector(manualRefresh))
        revealBackupButton = button("查看备份", #selector(revealBackup))
        progress = NSProgressIndicator(); progress.style = .spinning; progress.controlSize = .small
        progress.isDisplayedWhenStopped = false
        let actions = NSStackView(views: [backupButton, refreshButton, revealBackupButton, progress]); actions.spacing = 10
        stack.addArrangedSubview(actions)
        stack.addArrangedSubview(label("所有账号合计最多保留最近 2 份备份；新备份成功后自动清理旧份。", size: 11, secondary: true))
        let separator = NSBox(); separator.boxType = .separator; stack.addArrangedSubview(separator)

        stack.addArrangedSubview(label("将备份导入当前账号", size: 14, weight: .semibold))
        stack.addArrangedSubview(label("先在 Claude 登录目标账号，再选择备份导入。", size: 11, secondary: true))
        backupPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        backupPopup.addItem(withTitle: "暂无保存的备份")
        backupPopup.target = self; backupPopup.action = #selector(backupSelectionChanged)
        backupPopup.menu?.delegate = self
        targetLabel = label("导入到：尚未识别", size: 12)
        let backupRow = NSStackView(views: [label("选择备份", size: 12), backupPopup, targetLabel]); backupRow.spacing = 12
        stack.addArrangedSubview(backupRow)
        summaryLabel = label("选择保存的备份后预览。预览不会修改 Claude 记录。", size: 12)
        stack.addArrangedSubview(summaryLabel)

        table = NSTableView(); table.dataSource = self; table.delegate = self
        table.usesAlternatingRowBackgroundColors = true; table.rowHeight = 25
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        for (key, title, width) in [("title", "记录", 300.0), ("kind", "处理", 95.0), ("reason", "说明", 290.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(key))
            column.title = title; column.width = width; column.minWidth = 70; table.addTableColumn(column)
        }
        let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.hasHorizontalScroller = true
        scroll.borderType = .bezelBorder; scroll.documentView = table
        stack.addArrangedSubview(scroll)
        previewButton = button("预览导入", #selector(previewSync))
        syncButton = button("导入到当前账号", #selector(confirmSync))
        let syncActions = NSStackView(views: [previewButton, syncButton]); syncActions.spacing = 10
        stack.addArrangedSubview(syncActions)
        messageLabel = label(message, size: 11, secondary: true)
        messageLabel.maximumNumberOfLines = 2
        stack.addArrangedSubview(messageLabel)
        let content = window.contentView!; content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -22),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 17),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -17),
            accountsSummary.widthAnchor.constraint(equalTo: stack.widthAnchor),
            separator.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor),
            scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 105),
            summaryLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            messageLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            currentLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
            backupPopup.widthAnchor.constraint(equalToConstant: 385),
            progress.widthAnchor.constraint(equalToConstant: 16),
            progress.heightAnchor.constraint(equalToConstant: 16)
        ])
        window.center(); self.window = window
    }

    private func setActivity(_ activity: Activity) {
        self.activity = activity; render(); activityChanged()
    }

    private func setMessage(_ text: String, error: Bool = false) {
        message = text; messageIsError = error; statusErrorVisible = false; render()
    }

    private func invalidatePreview() { preview = nil; previewVersion = nil; table?.reloadData() }

    @objc private func manualRefresh() { invalidatePreview(); refreshStatus() }

    private func updateBackupPicker(_ result: [String: Any]) {
        let previous = selectedBackup
        savedBackups = (result["backups"] as? [[String: Any]] ?? []).compactMap(SavedHistoryBackup.init)
        backupPopup.removeAllItems()
        if savedBackups.isEmpty {
            backupPopup.addItem(withTitle: "暂无保存的备份，请先备份当前账号")
        } else {
            for backup in savedBackups {
                backupPopup.addItem(withTitle: "\(backup.source) · \(formatDate(backup.createdAt, full: true)) · \(backup.records) 条")
                backupPopup.lastItem?.representedObject = backup.id
            }
            let preferred = savedBackups.first { $0.path == preferredBackupPath } ??
                savedBackups.first { $0.id == previous?.id } ?? savedBackups[0]
            if let index = savedBackups.firstIndex(of: preferred) { backupPopup.selectItem(at: index) }
        }
        preferredBackupPath = nil
        if previous != selectedBackup { invalidatePreview() }
    }

    private func refreshStatus() {
        guard activity == .idle && !backupMenuOpen else { return }
        setActivity(.status)
        backend.call("status", delayed: {}) { [weak self] result in
            guard let self = self else { return }
            let oldAccount = self.activeAlias
            let oldVersion = self.status?["version"] as? String
            if result["accounts"] is [[String: Any]], result["supported_version"] is Bool {
                self.status = result; self.statusReceived = Date()
                self.updateBackupPicker(result)
                if oldAccount != self.activeAlias || oldVersion != result["version"] as? String {
                    self.invalidatePreview()
                }
                if result["ok"] as? Bool != true || self.activeAlias == nil || !self.supported {
                    self.invalidatePreview()
                    self.message = result["detail"] as? String ?? "请先在 Claude 中完成登录，再刷新当前账号。"
                    self.messageIsError = true; self.statusErrorVisible = true
                } else if self.statusErrorVisible {
                    self.message = "当前状态已刷新。可备份当前账号，或选择保存的备份预览导入。"
                    self.messageIsError = false; self.statusErrorVisible = false
                }
            } else {
                self.status = nil; self.statusReceived = nil; self.invalidatePreview(); self.updateBackupPicker([:])
                self.message = result["detail"] as? String ?? "当前状态读取失败，请稍后刷新。"
                self.messageIsError = true; self.statusErrorVisible = true
            }
            self.setActivity(.idle)
        }
    }

    private func render() {
        guard window != nil else { return }
        let version = status?["version"] as? String ?? "未知"
        let running = status?["claude_running"] as? Bool
        let runningText = running.map { $0 ? "已打开" : "未运行" } ?? "状态未知"
        currentLabel.stringValue = "当前账号：\(activeAlias ?? "尚未识别")    Claude \(version) · \(runningText)"
        currentLabel.textColor = activeAlias == nil || !supported ? .secondaryLabelColor : .labelColor
        let accounts = status?["accounts"] as? [[String: Any]] ?? []
        accountsSummary.stringValue = accounts.compactMap { account -> String? in
            guard let alias = account["alias"] as? String, validHistoryAlias(alias) else { return nil }
            let count = (account["sessions"] as? Int).map { "\($0) 条" } ?? "无法读取"
            return "\(alias) · \(count)" + ((account["error"] as? String) == nil ? "" : " · 异常")
        }.joined(separator: "    ")
        if accounts.isEmpty { accountsSummary.stringValue = "尚未登记账号，请按 README 完成本机登记后刷新。" }
        accountsSummary.toolTip = accountsSummary.stringValue
        targetLabel.stringValue = "导入到：\(activeAlias ?? "尚未识别")"
        if let preview = preview {
            summaryLabel.stringValue = "新增 \(preview.missing + preview.continuation) 条（备份副本 \(preview.continuation)） · 补回 \(preview.restoreFiles) 个文件 · 保留 \(preview.retainedNewer) 个较新文件\n现有入口 \(preview.unchanged) · 相同 \(preview.equivalent) · 跳过 \(preview.skipped) · 冲突 \(preview.conflicts)"
            summaryLabel.textColor = preview.conflicts > 0 ? .systemRed : .labelColor
        } else {
            summaryLabel.stringValue = savedBackups.isEmpty ?
                "暂无保存的备份。点击“备份当前账号”创建第一份备份。" : "点击“预览导入”检查所选备份。预览不会修改 Claude 记录。"
            summaryLabel.textColor = .secondaryLabelColor
        }
        let ready = canOperate && statusIsFresh && status?["ok"] as? Bool == true && supported && activeAlias != nil
        backupButton.isEnabled = ready
        refreshButton.isEnabled = activity == .idle
        revealBackupButton.isEnabled = activity == .idle && backupPath != nil
        revealBackupButton.toolTip = backupPath.map { "在 Finder 中显示备份：\($0)" } ?? "完成备份后可在 Finder 中查看"
        backupPopup.isEnabled = activity == .idle && !savedBackups.isEmpty
        previewButton.isEnabled = ready && selectedBackup != nil
        syncButton.isEnabled = ready && (preview?.canApply(backupID: selectedBackupID, currentAccount: activeAlias, supported: supported) == true)
        if activity == .idle { progress.stopAnimation(nil) } else { progress.startAnimation(nil) }
        messageLabel.stringValue = message
        messageLabel.textColor = messageIsError ? .systemRed : .secondaryLabelColor
        messageLabel.toolTip = message
    }

    private func formatDate(_ raw: String?, full: Bool = false) -> String {
        guard let raw = raw, !raw.isEmpty else { return "暂无" }
        let parser = ISO8601DateFormatter()
        var date = parser.date(from: raw)
        if date == nil { parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; date = parser.date(from: raw) }
        guard let date = date else { return raw }
        let formatter = DateFormatter(); formatter.dateFormat = full ? "yyyy-MM-dd HH:mm:ss" : "MM-dd HH:mm"
        return formatter.string(from: date)
    }

    @objc private func backupSelectionChanged() {
        invalidatePreview()
        setMessage("已选择保存的备份。点击“预览导入”，检查新增记录后再导入。")
    }

    @objc private func backupCurrent() {
        guard canOperate, statusIsFresh, status?["ok"] as? Bool == true, supported, let source = activeAlias else { return }
        invalidatePreview(); setActivity(.backup)
        setMessage("正在备份 \(source) 的本地记录，请等待完成…")
        backend.call("backup", request: ["source": source], delayed: { [weak self] in
            self?.setMessage("备份仍在进行，记录较多时需要更长时间。请保持本应用打开；网络监测继续独立运行。")
        }) { [weak self] result in
            guard let self = self else { return }
            self.setActivity(.idle)
            if result["ok"] as? Bool == true {
                self.lastBackupPath = result["path"] as? String
                self.preferredBackupPath = self.lastBackupPath
                let records = result["records"] as? Int ?? 0
                let bytes = (result["bytes"] as? NSNumber)?.int64Value ?? 0
                let size = ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
                let warnings = (result["retention"] as? [String: Any])?["warnings"] as? [String] ?? []
                let detail = warnings.isEmpty ? "所有账号合计最多保留最近 2 份备份。" : warnings.joined(separator: "；")
                self.setMessage("已备份 \(source)：\(records) 条记录，\(size)。" + detail, error: !warnings.isEmpty)
            } else { self.setMessage(result["detail"] as? String ?? "备份未完成，请检查后重试。", error: true) }
            self.refreshStatus()
        }
    }

    @objc private func revealBackup() {
        guard activity == .idle, let path = backupPath else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    private func openClaude(successMessage: String?, completion: (() -> Void)? = nil) {
        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/Claude.app"),
            configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
            DispatchQueue.main.async {
                if let error = error {
                    self?.setMessage("Claude 未能打开：\(error.localizedDescription)。请手动打开 /Applications/Claude.app。", error: true)
                } else if let message = successMessage { self?.setMessage(message) }
                completion?()
            }
        }
    }

    @objc private func previewSync() {
        guard canOperate, statusIsFresh, status?["ok"] as? Bool == true, supported,
              let backup = selectedBackup, let target = activeAlias else { return }
        let expectedVersion = status?["version"] as? String
        invalidatePreview(); setActivity(.preview)
        setMessage("正在检查 \(backup.source) 的保存备份导入 \(target) 后的变化…")
        backend.call("preview", request: ["backup_id": backup.id, "target": target], delayed: {}) { [weak self] result in
            guard let self = self else { return }
            self.preview = HistoryPreview(result); self.previewVersion = expectedVersion; self.table.reloadData()
            self.setActivity(.idle)
            if let preview = self.preview {
                self.setMessage(preview.detail.isEmpty ? "请检查上方预览；导入前会再次确认并正常退出 Claude。" : preview.detail,
                                error: preview.conflicts > 0)
            } else { self.setMessage(result["detail"] as? String ?? "预览结果不完整，请刷新后重试。", error: true) }
            // A long preview must not reuse an expired active-account observation.
            if !self.statusIsFresh { self.refreshStatus() }
        }
    }

    @objc private func confirmSync() {
        guard canOperate, statusIsFresh, let preview = preview,
              preview.canApply(backupID: selectedBackupID, currentAccount: activeAlias, supported: supported),
              status?["ok"] as? Bool == true, let version = previewVersion, version == status?["version"] as? String,
              let window = window else { return }
        setActivity(.confirming)
        let alert = NSAlert()
        alert.messageText = "将所选备份导入当前账号 \(preview.target)？"
        alert.informativeText = "备份来源：\(preview.source) · \(formatDate(preview.backupCreatedAt, full: true))\n新增 \(preview.missing + preview.continuation) 条记录（其中独立备份副本 \(preview.continuation) 条）；补回 \(preview.restoreFiles) 个缺失正文或附属文件；保留 \(preview.retainedNewer) 个较新文件。\n\n遇到内容冲突时停止，不覆盖现有记录。导入前会先备份当前账号。\n\n接下来会正常退出 Claude，当前后台任务可能中断。完成后会重新打开 Claude。新增入口使用手动权限，不会自动续跑；已有会话保留原设置，可能按 Claude 原有设置恢复任务。"
        alert.addButton(withTitle: "取消"); alert.addButton(withTitle: "退出 Claude 并导入")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self = self else { return }
            guard response == .alertSecondButtonReturn else { self.setActivity(.idle); return }
            self.performSync(preview, expectedVersion: version)
        }
    }

    private func performSync(_ preview: HistoryPreview, expectedVersion: String) {
        // Login may have changed while the confirmation sheet was open. Verify
        // again before terminating anything, not only when the backend writes.
        setActivity(.quitting)
        setMessage("正在重新核对当前账号与版本…")
        backend.call("status", delayed: {}) { [weak self] result in
            guard let self = self else { return }
            let backups = (result["backups"] as? [[String: Any]] ?? []).compactMap(SavedHistoryBackup.init)
            guard historyIdentityMatches(result, target: preview.target, version: expectedVersion),
                  backups.contains(where: { $0.id == preview.backupID && $0.source == preview.source }) else {
                self.invalidatePreview(); self.statusReceived = nil
                self.setActivity(.idle)
                self.setMessage("账号、版本或所选备份与预览不一致，尚未退出 Claude，也没有导入。请刷新并重新预览。", error: true)
                self.refreshStatus(); return
            }
            self.status = result; self.statusReceived = Date()
            self.quitClaudeAndSync(preview)
        }
    }

    private func quitClaudeAndSync(_ preview: HistoryPreview) {
        setActivity(.quitting)
        setMessage("正在等待 Claude 正常退出；不会强制结束进程…")
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.anthropic.claudefordesktop")
            .filter { $0.bundleURL?.standardizedFileURL.path == "/Applications/Claude.app" }
        for app in apps { _ = app.terminate() }
        waitForExit(apps, deadline: Date().addingTimeInterval(20)) { [weak self] closed in
            guard let self = self else { return }
            guard closed else {
                self.setActivity(.idle); self.invalidatePreview()
                self.setMessage("Claude 尚未退出，没有导入记录。请保存或结束当前任务、正常退出 Claude 后重新预览。", error: true)
                self.refreshStatus(); return
            }
            self.setActivity(.sync)
            self.setMessage("正在备份当前账号并导入所选备份，请保持本应用打开…")
            self.backend.call("sync", request: ["backup_id": preview.backupID, "target": preview.target, "preview_id": preview.id],
                delayed: { [weak self] in
                    self?.setMessage("导入仍在进行，记录较多时需要更长时间。请保持本应用打开；网络监测继续独立运行。")
                }) { [weak self] result in
                    guard let self = self else { return }
                    self.invalidatePreview()
                    if result["ok"] as? Bool == true {
                        self.lastBackupPath = result["backup_path"] as? String ?? self.lastBackupPath
                        let added = result["added"] as? Int ?? 0
                        let restored = result["restored_files"] as? Int ?? 0
                        let warnings = (result["retention"] as? [String: Any])?["warnings"] as? [String] ?? []
                        let message = "已导入 \(added) 条记录、补回 \(restored) 个文件到 \(preview.target)。Claude 已重新打开，请在 Code 列表查看。" + warnings.joined(separator: "；")
                        self.setActivity(.reopening)
                        self.setMessage("导入完成：新增 \(added) 条记录、补回 \(restored) 个文件。正在重新打开 Claude…")
                        self.openClaude(successMessage: message) { [weak self] in
                            self?.setActivity(.idle); self?.refreshStatus()
                        }
                    } else {
                        self.setActivity(.idle)
                        let detail = result["detail"] as? String ?? "导入未完成。"
                        self.setMessage(detail + " Claude 保持关闭，请点击“预览导入”检查当前记录后再次确认。", error: true)
                        self.refreshStatus()
                    }
                }
        }
    }

    private func waitForExit(_ apps: [NSRunningApplication], deadline: Date, completion: @escaping (Bool) -> Void) {
        if apps.allSatisfy({ $0.isTerminated }) { completion(true); return }
        if Date() >= deadline { completion(false); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            self?.waitForExit(apps, deadline: deadline, completion: completion)
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if isMutationInProgress { setMessage(exitBlockedReason); return false }
        return true
    }

    func menuWillOpen(_ menu: NSMenu) { backupMenuOpen = true }
    func menuDidClose(_ menu: NSMenu) { backupMenuOpen = false }

    func numberOfRows(in tableView: NSTableView) -> Int { preview?.items.count ?? 0 }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard let items = preview?.items, items.indices.contains(row), let key = tableColumn?.identifier.rawValue else { return nil }
        let item = items[row]
        var text = item[key] as? String ?? ""
        if key == "kind" {
            text = ["missing": "补齐", "continuation": "备份副本", "unchanged": "已存在", "equivalent": "相同记录", "archived": "已归档，跳过", "deleted": "已删除，跳过", "skipped": "跳过", "conflict": "冲突"][text] ?? text
        }
        let cell = NSTextField(labelWithString: text)
        cell.font = .systemFont(ofSize: 11); cell.lineBreakMode = .byTruncatingTail; cell.toolTip = text
        if item["kind"] as? String == "conflict" { cell.textColor = .systemRed }
        return cell
    }
}

func runHistorySelfTests() {
    let base: [String: Any] = ["ok": true, "preview_id": "test-plan", "backup_id": "alpha/saved-a",
        "backup_created_at": "2026-01-01T12:00:00Z", "source": "alpha", "target": "beta",
        "missing_count": 2, "continuation_count": 1, "unchanged_count": 3, "equivalent_count": 0,
        "skipped_count": 0, "conflict_count": 0, "can_sync": true, "items": []]
    precondition(!validHistoryAlias("personal\n"))
    precondition(validHistoryAlias("my-work_2"))
    var count = 2
    func expect(_ changes: [String: Any] = [:], backupID: String? = "alpha/saved-a", target: String? = "beta",
                supported: Bool = true, allowed: Bool) {
        let preview = HistoryPreview(base.merging(changes) { _, new in new })
        precondition((preview?.canApply(backupID: backupID, currentAccount: target, supported: supported) ?? false) == allowed)
        count += 1
    }
    expect(allowed: true)
    expect(["conflict_count": 1], allowed: false)
    expect(["can_sync": false], allowed: false)
    expect(["missing_count": 0, "continuation_count": 0], allowed: false)
    expect(["missing_count": 0, "continuation_count": 0, "restore_file_count": 2], allowed: true)
    expect(["missing_count": 0, "continuation_count": 0, "retained_newer_count": 2], allowed: false)
    expect(["restore_file_count": -1], allowed: false)
    expect(["missing_count": 0], allowed: true)
    expect(backupID: "gamma/saved-a", allowed: false)
    expect(backupID: "alpha/saved-b", allowed: false)
    expect(backupID: nil, allowed: false)
    expect(["backup_id": ""], allowed: false)
    expect(target: "gamma", allowed: false)
    expect(target: nil, allowed: false)
    expect(supported: false, allowed: false)
    expect(["preview_id": ""], allowed: false)
    expect(["missing_count": -1], allowed: false)
    expect(["ok": false], allowed: false)
    expect(["target": "alpha"], target: "alpha", allowed: true)
    let identity: [String: Any] = ["ok": true, "active_alias": "beta", "version": "test-version", "supported_version": true]
    for (changes, expected) in [([String: Any](), true),
        (["active_alias": "gamma"], false), (["version": "changed"], false),
        (["supported_version": false], false), (["ok": false], false), (["active_alias": NSNull()], false)] {
        precondition(historyIdentityMatches(identity.merging(changes) { _, new in new },
                     target: "beta", version: "test-version") == expected)
        count += 1
    }
    print("history_ui_tests_passed=\(count)")
}

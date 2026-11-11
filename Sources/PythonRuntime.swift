import Foundation

// Finder does not inherit a terminal PATH. Support both Homebrew architectures
// and an explicit override without bundling a machine-specific interpreter path.
enum PythonRuntime {
    static let executable: URL? = {
        let override = ProcessInfo.processInfo.environment["CLAUDE_COMPANION_PYTHON"]
        let candidates = override.map { [$0] } ?? [
            "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"
        ]
        for path in candidates {
            guard path.hasPrefix("/"), FileManager.default.isExecutableFile(atPath: path) else { continue }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = ["-B", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"]
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                let deadline = Date().addingTimeInterval(5)
                while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
                if process.isRunning { process.terminate(); continue }
                if process.terminationStatus == 0 { return URL(fileURLWithPath: path) }
            } catch { continue }
        }
        return nil
    }()
}

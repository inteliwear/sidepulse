// Observe dictation input activity, then meter that input locally. No audio is saved.
import AppKit
import AVFoundation
import AudioToolbox
import CoreAudio

let targets = [
    "com.electron.wispr-flow": "Wispr Flow",
    "com.gizmolabs.openwhispr": "OpenWhispr",
    "org.openwispr.app": "OpenWispr",
    "dev.openwispr.app": "OpenWispr"
]

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope,
                               mElement: kAudioObjectPropertyElementMain)
}

func uintProperty(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var key = address(selector)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(id, &key, 0, nil, &size, &value) == noErr else { return nil }
    return value
}

func idsProperty(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector,
                 _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) throws -> [AudioObjectID] {
    var key = address(selector, scope)
    var size: UInt32 = 0
    var status = AudioObjectGetPropertyDataSize(id, &key, 0, nil, &size)
    guard status == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    if size == 0 { return [] }
    var values = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    status = values.withUnsafeMutableBytes {
        AudioObjectGetPropertyData(id, &key, 0, nil, &size, $0.baseAddress!)
    }
    guard status == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    return Array(values.prefix(Int(size) / MemoryLayout<AudioObjectID>.size))
}

func bundleProperty(_ id: AudioObjectID) -> String? {
    var key = address(kAudioProcessPropertyBundleID)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &key, 0, nil, &size, &value) == noErr,
          let value = value else { return nil }
    // Core Audio transfers ownership of this CFString to the caller.
    return value.takeRetainedValue() as String
}

func targetForBundle(_ value: String) -> String? {
    let bundle = value.lowercased()
    return targets.keys.sorted().first(where: { bundle == $0 || bundle.hasPrefix($0 + ".") })
}

struct DictationInput {
    let app: String
    let device: AudioObjectID?
}

func dictationInput() throws -> DictationInput? {
    let objects = try idsProperty(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList)
    for object in objects {
        guard uintProperty(object, kAudioProcessPropertyIsRunningInput) == 1 else { continue }
        guard let target = targetForBundle(bundleProperty(object) ?? "") else { continue }
        let devices = try idsProperty(object, kAudioProcessPropertyDevices, kAudioObjectPropertyScopeInput)
        return DictationInput(app: targets[target]!, device: devices.first)
    }
    return nil
}

func emit(_ active: Bool, _ level: Double = 0, _ app: String = "", _ error: String = "", _ tone: Double = 0.5) {
    let payload: [String: Any] = ["active": active, "level": level, "app": app, "error": error, "tone": tone]
    if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

// Eight sparse spectral probes estimate vocal timbre, not linguistic content or speaker identity.
func estimateVocalTone(_ samples: UnsafePointer<Float>, _ length: Int, _ sampleRate: Double) -> Double {
    let stride = max(1, Int(sampleRate / 12000))
    let rate = sampleRate / Double(stride)
    let count = min(length, 4096)
    var total = 0.0
    var weighted = 0.0
    for frequency in [160.0, 250, 400, 630, 1000, 1600, 2500, 4000] where frequency < rate * 0.45 {
        let coefficient = 2 * cos(2 * Double.pi * frequency / rate)
        var a = 0.0
        var b = 0.0
        var frame = 0
        while frame < count {
            let next = Double(samples[frame]) + coefficient * a - b
            b = a
            a = next
            frame += stride
        }
        let energy = max(0, a * a + b * b - coefficient * a * b)
        total += energy
        weighted += energy * frequency
    }
    guard total > 0.000001 else { return 0.5 }
    let centroid = weighted / total
    return min(1, max(0, log2(centroid / 200) / log2(2500 / 200)))
}

final class Meter {
    private var engine: AVAudioEngine?
    private let lock = NSLock()
    private var rms: Double = 0
    private var tone: Double = 0.5
    private var sampleTime: TimeInterval = 0
    private var permissionRequested = false
    private var inputDevice: AudioObjectID?
    private var retryAfter: TimeInterval = 0
    private(set) var error = ""

    func start(device: AudioObjectID?) {
        if engine != nil && device == inputDevice { return }
        if engine != nil { stop() }
        guard ProcessInfo.processInfo.systemUptime >= retryAfter else { return }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .notDetermined:
            error = "Allow microphone access to animate speech volume."
            if !permissionRequested {
                permissionRequested = true
                AVCaptureDevice.requestAccess(for: .audio) { _ in }
            }
            return
        case .authorized: break
        default:
            error = "Enable microphone access for SidePulse Voice in System Settings."
            return
        }
        let next = AVAudioEngine()
        let input = next.inputNode
        if var device = device, let unit = input.audioUnit {
            let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
                                             kAudioUnitScope_Global, 0, &device,
                                             UInt32(MemoryLayout<AudioObjectID>.size))
            if status != noErr {
                error = "Could not meter the dictation microphone (\(status))."
                retryAfter = ProcessInfo.processInfo.systemUptime + 1
                return
            }
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 && format.channelCount > 0 else {
            error = "The dictation microphone is unavailable."
            retryAfter = ProcessInfo.processInfo.systemUptime + 1
            return
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self = self, let channels = buffer.floatChannelData, buffer.frameLength > 0 else { return }
            var peakRMS: Double = 0
            var measuredTone = 0.5
            for channel in 0..<Int(buffer.format.channelCount) {
                var squares: Double = 0
                for frame in 0..<Int(buffer.frameLength) {
                    let value = Double(channels[channel][frame])
                    squares += value * value
                }
                let channelRMS = sqrt(squares / Double(buffer.frameLength))
                if channelRMS > peakRMS {
                    peakRMS = channelRMS
                    measuredTone = estimateVocalTone(channels[channel], Int(buffer.frameLength), buffer.format.sampleRate)
                }
            }
            self.lock.lock()
            self.rms = peakRMS
            self.tone = measuredTone
            self.sampleTime = ProcessInfo.processInfo.systemUptime
            self.lock.unlock()
        }
        do {
            try next.start()
            engine = next
            inputDevice = device
            error = ""
        } catch {
            input.removeTap(onBus: 0)
            self.error = "Microphone metering failed: \(error.localizedDescription)"
            retryAfter = ProcessInfo.processInfo.systemUptime + 1
        }
    }

    func level() -> Double {
        lock.lock()
        let value = rms
        let age = ProcessInfo.processInfo.systemUptime - sampleTime
        lock.unlock()
        guard age < 0.25 else { return 0 }
        let db = 20 * log10(max(value, 0.0000001))
        return min(1, max(0, (db + 55) / 40))
    }

    func vocalTone() -> Double {
        lock.lock()
        let value = tone
        lock.unlock()
        return value
    }

    func stop() {
        if let current = engine {
            current.stop()
            current.inputNode.removeTap(onBus: 0)
        }
        engine = nil
        inputDevice = nil
        error = ""
        lock.lock()
        rms = 0
        tone = 0.5
        sampleTime = 0
        lock.unlock()
    }
}

guard #available(macOS 14.2, *) else {
    emit(false, 0, "", "Voice visualization requires macOS 14.2 or later.")
    exit(1)
}
if CommandLine.arguments.contains("--selftest") {
    precondition(targetForBundle("com.electron.wispr-flow") != nil)
    precondition(targetForBundle("com.electron.wispr-flow.helper.renderer") != nil)
    precondition(targetForBundle("com.gizmolabs.openwhispr.helper") != nil)
    precondition(targetForBundle("com.electron.wispr-flow-unrelated") == nil)
    precondition(targetForBundle("com.apple.FaceTime") == nil)
    precondition(targetForBundle("io.sidepulse.voice") == nil)
    precondition(targetForBundle("") == nil)
    let low = (0..<4096).map { Float(sin(2 * Double.pi * 160 * Double($0) / 48000)) }
    let high = (0..<4096).map { Float(sin(2 * Double.pi * 2500 * Double($0) / 48000)) }
    let lowTone = low.withUnsafeBufferPointer { estimateVocalTone($0.baseAddress!, low.count, 48000) }
    let highTone = high.withUnsafeBufferPointer { estimateVocalTone($0.baseAddress!, high.count, 48000) }
    precondition(highTone > lowTone + 0.3)
    print("Voice target matching and spectral tone passed.")
    exit(0)
}
if CommandLine.arguments.contains("--probe") {
    do {
        if let active = try dictationInput() { emit(true, 0, active.app) }
        else { emit(false) }
    } catch { emit(false, 0, "", "Could not inspect microphone activity: \(error)"); exit(1) }
    exit(0)
}
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let meter = Meter()
let timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
    // Exit if the owning companion died, including a crash or SIGKILL.
    if getppid() == 1 { meter.stop(); exit(0) }
    do {
        if let active = try dictationInput() {
            meter.start(device: active.device)
            emit(true, meter.level(), active.app, meter.error, meter.vocalTone())
        } else {
            meter.stop()
            emit(false)
        }
    } catch {
        meter.stop()
        emit(false, 0, "", "Could not inspect microphone activity: \(error)")
    }
}
application.run()

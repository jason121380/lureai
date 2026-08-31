import AVFoundation
import Foundation
import Vision

guard CommandLine.arguments.count == 2 else {
    fputs("usage: video_frame_ocr <video>\n", stderr)
    exit(2)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let asset = AVAsset(url: url)
let duration = CMTimeGetSeconds(asset.duration)
guard duration.isFinite && duration > 0 else {
    fputs("cannot read video duration\n", stderr)
    exit(1)
}

let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = CMTime(seconds: 0.5, preferredTimescale: 600)
generator.requestedTimeToleranceAfter = CMTime(seconds: 0.5, preferredTimescale: 600)

let interval = max(5.0, duration / 24.0)
var seen = Set<String>()
var second = 0.0

while second < duration {
    let time = CMTime(seconds: second, preferredTimescale: 600)
    do {
        let image = try generator.copyCGImage(at: time, actualTime: nil)
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.recognitionLanguages = ["zh-Hant", "en-US"]
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        let text = lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty && !seen.contains(text) {
            seen.insert(text)
            print("### \(Int(second)) 秒")
            print(text)
            print("")
        }
    } catch {
        fputs("frame \(second) failed: \(error)\n", stderr)
    }
    second += interval
}

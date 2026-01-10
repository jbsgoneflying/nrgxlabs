import Foundation
import Combine

@MainActor
final class SPXViewModel: ObservableObject {
    @Published var isLoading = false
    @Published var error: AppError?
    @Published var flags: FlagsResponse?

    // Controls (match web defaults)
    @Published var underlying: String = "SPX" // SPX|SPY|QQQ
    @Published var entryDay: String = "mon"   // mon|tue|wed
    @Published var seasonalityMode: String = "none" // none|quarter|month|summer|opex

    // Outputs
    @Published var ic: SPXICResponse?
    @Published var levels: SPXLevelsResponse?

    @Published var icSummary: String = "Not loaded"
    @Published var levelsSummary: String = "Not loaded"

    func load(client: APIClient) async {
        isLoading = true
        self.error = nil
        defer { isLoading = false }
        do {
            // Feature gate + defaults
            let f: FlagsResponse = try await client.get("api/flags", timeout: 20)
            self.flags = f
            if f.enableEngine2SpxIc == false {
                ic = nil
                levels = nil
                icSummary = "Engine 2 disabled (ENABLE_ENGINE2_SPX_IC=0)"
                levelsSummary = "Engine 2 disabled (ENABLE_ENGINE2_SPX_IC=0)"
                return
            }

            let years = String(f.engine2DefaultYears ?? 2)
            let widths = f.engine2DefaultEmMults ?? "1.0,1.5,2.0"

            // /api/spx-ic (Engine 2 core) can be heavy; give it more time than the shared 45s.
            ic = try await client.get(
                "api/spx-ic",
                query: [
                    "underlying": underlying,
                    "entry_day": entryDay,
                    "years": years,
                    "widths": widths,
                    "seasonality_mode": seasonalityMode,
                    "weeks_limit": "0", // keep payload lightweight like web initial render
                ],
                timeout: 120
            )
            icSummary = summarizeIC(ic) ?? "Loaded (/api/spx-ic)"

            // /api/spx-levels (dealer gamma + heatmap) can also be non-trivial.
            levels = try await client.get(
                "api/spx-levels",
                query: [
                    "underlying": underlying,
                    "view": "weekly",
                    "points": "90",
                    "window_days": "180",
                    "include_heatmap": "1",
                    "heatmap_view": "composite",
                    "heatmap_mode": "slope",
                    "slope_window": "5",
                    "flip_adjacent_n": "5",
                ],
                timeout: 90
            )
            levelsSummary = summarizeLevels(levels) ?? "Loaded (/api/spx-levels)"
        } catch let appError as AppError {
            self.error = appError
        } catch {
            self.error = .network(error)
        }
    }

    private func summarizeIC(_ resp: SPXICResponse?) -> String? {
        guard let r = resp else { return nil }
        let sym = r.underlying?.symbol ?? "—"
        let proxy = (r.underlying?.isProxy == true) ? " (proxy)" : ""
        let asOf = (r.asOfDate ?? "").isEmpty ? "—" : (r.asOfDate ?? "—")
        let bucket = r.current?.regime?.bucket ?? "—"
        let score = r.current?.regime?.score100
        let scoreTxt = score.map { String(format: "%.1f", $0) } ?? "—"
        return "\(sym)\(proxy) · asOf \(asOf) · regime \(bucket) (\(scoreTxt)/100)"
    }

    private func summarizeLevels(_ resp: SPXLevelsResponse?) -> String? {
        guard let r = resp else { return nil }
        let v = r.levels?.view ?? "—"
        let sym = r.levels?.symbolUsed ?? "—"
        let exp = (r.levels?.expiry ?? "").isEmpty ? "—" : (r.levels?.expiry ?? "—")
        let spot = r.levels?.spot
        let spotTxt = spot.map { String(format: "%.2f", $0) } ?? "—"
        return "\(sym) · \(v) · expiry \(exp) · spot \(spotTxt)"
    }
}

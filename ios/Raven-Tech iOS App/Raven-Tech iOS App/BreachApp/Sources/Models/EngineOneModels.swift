import Foundation

// MARK: - Engine 1 (Breach) API Response Models

struct BreachResponse: Decodable {
    var enabled: Bool?
    var schemaVersion: Int?
    var ticker: String?
    var asOfDate: String?
    var underlyingPrice: Double?
    var summary: BreachSummary?
    var quarters: [String: QuarterStats]?
    var events: [BreachEvent]
    var wingRecommendation: WingRecommendation?
    var regime: RegimeData?
    var goNoGo: GoNoGoDecision?
    var current: CurrentSnapshot?
    var expectedMove: ExpectedMove?
    var nextEvent: NextEvent?

    enum CodingKeys: String, CodingKey {
        case enabled, schemaVersion, ticker, asOfDate, underlyingPrice
        case summary, quarters, events, wingRecommendation, regime, goNoGo
        case current, expectedMove, nextEvent
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.enabled = try? c.decodeIfPresent(Bool.self, forKey: .enabled)
        self.schemaVersion = try? c.decodeIfPresent(Int.self, forKey: .schemaVersion)
        self.ticker = try? c.decodeIfPresent(String.self, forKey: .ticker)
        self.asOfDate = try? c.decodeIfPresent(String.self, forKey: .asOfDate)
        self.underlyingPrice = try? c.decodeIfPresent(Double.self, forKey: .underlyingPrice)
        self.summary = try? c.decodeIfPresent(BreachSummary.self, forKey: .summary)
        self.quarters = try? c.decodeIfPresent([String: QuarterStats].self, forKey: .quarters)
        self.events = (try? c.decode([BreachEvent].self, forKey: .events)) ?? []
        self.wingRecommendation = try? c.decodeIfPresent(WingRecommendation.self, forKey: .wingRecommendation)
        self.regime = try? c.decodeIfPresent(RegimeData.self, forKey: .regime)
        do {
            self.goNoGo = try c.decodeIfPresent(GoNoGoDecision.self, forKey: .goNoGo)
        } catch {
            print("Go/No-Go decode failed: \(error)")
            self.goNoGo = nil
        }
        self.current = try? c.decodeIfPresent(CurrentSnapshot.self, forKey: .current)
        self.expectedMove = try? c.decodeIfPresent(ExpectedMove.self, forKey: .expectedMove)
        self.nextEvent = try? c.decodeIfPresent(NextEvent.self, forKey: .nextEvent)
    }
}

// MARK: - Current Snapshot (ORATS impErnMv)

/// Current price/EM snapshot from ORATS cores endpoint.
/// The `impliedMovePct` here is ORATS's implied earnings move (impErnMv),
/// which is used for historical earnings event calculations.
struct CurrentSnapshot: Decodable {
    var asOfDate: String?
    var stockPrice: Double?
    var impErnMv: Double?
    var impliedMovePct: Double?
    var source: String?
}

// MARK: - Expected Move (ATM-forward straddle calculation)

/// Expected move computed from ATM-forward straddle method.
/// This is our calculated EM from the option chain.
struct ExpectedMove: Decodable {
    var ticker: String?
    var asOfDate: String?
    var expiry: String?
    var dte: Int?
    var source: String?
    var spotPrice: Double?
    var forwardPrice: Double?
    var straddlePV: Double?
    var expectedMoveDollars: Double?
    var expectedMovePct: Double?
    var discountFactor: Double?
    var riskFreeRate: Double?
    var strikesUsedForForward: Int?
    var warnings: [String]?
}

// MARK: - Next Event

/// Next earnings event metadata and planned implied move.
struct NextEvent: Decodable {
    var earnDateNext: String?
    var timingPlanned: String?
    var pricingDatePlanned: String?
    var pricingDateTarget: String?
    var pricingDateAsOf: String?
    var impliedMovePctPlanned: Double?
    var impliedMoveSource: String?
    var source: String?
    var confidence: String?
    var notes: [String]?
}

struct BreachSummary: Decodable {
    var eventsFound: Int?
    var eventsUsed: Int?
    var breaches: Int?
    var breachRatePct: Double?
    var avgImpliedAllPct: Double?
    var avgRealizedAllPct: Double?
    var avgAboveBreachPct: Double?
    var avgRealizedIfBreachPct: Double?
    var avgUpOvershootPct: Double?
    var avgDownOvershootPct: Double?
    var upBreachRatePct: Double?
    var downBreachRatePct: Double?
    var upBreaches: Int?
    var downBreaches: Int?
    var tailBias: String?
}

/// Quarter stats from the backend (dictionary values)
struct QuarterStats: Decodable {
    var eventsTotal: Int?
    var eventsUsed: Int?
    var breaches: Int?
    var breachRatePct: Double?
    var avgRealizedAllPct: Double?
    var avgImpliedAllPct: Double?
    var quarterUpBreachRatePct: Double?
    var quarterDownBreachRatePct: Double?
    var recommendation: String?
}

struct BreachEvent: Decodable, Identifiable {
    var id: String { "\(earnDate ?? "")|\(timing ?? "")" }
    var earnDate: String?
    var timing: String?
    var anncTod: String?
    var impliedMovePct: Double?
    var realizedMovePct: Double?
    var breach: Bool?
    var moveDirection: String?
    var signedMovePct: Double?
    var closePx: Double?
    var openPx: Double?
    var upBreach: Bool?
    var downBreach: Bool?
    var breachSide: String?
}

struct WingRecommendation: Decodable {
    var mode: String?
    var callWingMultiple: Double?
    var putWingMultiple: Double?
    var callWidthEm: Double?
    var putWidthEm: Double?
    var recommendation: String?
    var recommendationLabel: String?
    var rationale: String?
    var tradeGate: String?
    var structureRationale: String?
    var notes: [String]?
}

struct RegimeData: Decodable {
    var asOfDate: String?
    var label: String?
    var tailMultiplier: Double?
    var scores: RegimeScores?
    var inputs: RegimeInputs?
    var guidance: RegimeGuidance?
    var tradeGate: String?

    // Convenience accessors to match UI expectations
    var bucket: String? { label }
    var score100: Double? {
        guard let s = scores?.regimeScore else { return nil }
        return s * 100  // Convert 0-1 to 0-100 scale
    }
}

struct RegimeScores: Decodable {
    var marketStress: Double?
    var singleNameVol: Double?
    var correlationProxy: Double?
    var regimeScore: Double?
}

struct RegimeInputs: Decodable {
    var spyRv20: Double?
    var spyRv20Percentile: Double?
    var tickerIv30: Double?
    var tickerIv30Percentile: Double?
    var spyAbsRet5d: Double?
    var spyAbsRet5dPercentile: Double?
}

struct RegimeGuidance: Decodable {
    var tradeGate: String?
    var message: String?
}

// MARK: - GO/NO-GO Decision

struct GoNoGoDecision: Decodable {
    var status: String?
    var passed: Bool?
    var checks: [GoNoGoCheck]?
    var warnings: [GoNoGoWarning]?
    var notes: [String]?

    enum CodingKeys: String, CodingKey {
        case status, passed, checks, warnings, notes
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.status = try? c.decodeIfPresent(String.self, forKey: .status)
        self.passed = try? c.decodeIfPresent(Bool.self, forKey: .passed)
        // Use try? to gracefully handle check decoding failures
        self.checks = try? c.decodeIfPresent([GoNoGoCheck].self, forKey: .checks)
        self.warnings = try? c.decodeIfPresent([GoNoGoWarning].self, forKey: .warnings)
        self.notes = try? c.decodeIfPresent([String].self, forKey: .notes)

        // Debug: log what we got
        print("🔍 GoNoGoDecision decoded: status=\(status ?? "nil"), passed=\(passed?.description ?? "nil"), checks=\(checks?.count ?? 0), warnings=\(warnings?.count ?? 0)")
    }
}

struct GoNoGoCheck: Decodable, Identifiable {
    var id: String { checkId ?? UUID().uuidString }
    var checkId: String?
    var label: String?
    var state: String?  // PASS, FAIL, WARN, MISSING
    var code: String?
    var explain: String?
    var value: [String: GoNoGoValue]?
    var threshold: [String: GoNoGoValue]?

    enum CodingKeys: String, CodingKey {
        case checkId = "id"
        case label, state, code, explain, value, threshold
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.checkId = try? c.decodeIfPresent(String.self, forKey: .checkId)
        self.label = try? c.decodeIfPresent(String.self, forKey: .label)
        self.state = try? c.decodeIfPresent(String.self, forKey: .state)
        self.code = try? c.decodeIfPresent(String.self, forKey: .code)
        self.explain = try? c.decodeIfPresent(String.self, forKey: .explain)
        // Use try? to gracefully handle deeply nested decoding failures
        self.value = try? c.decodeIfPresent([String: GoNoGoValue].self, forKey: .value)
        self.threshold = try? c.decodeIfPresent([String: GoNoGoValue].self, forKey: .threshold)
    }

    /// Render a short human-readable value summary for UI use
    var valueSummary: String? {
        guard let value else { return nil }
        let parts = value
            .filter { $0.value.shortDescription != "—" }  // Skip null values
            .map { "\($0.key): \($0.value.shortDescription)" }
            .sorted()
        return parts.isEmpty ? nil : parts.joined(separator: ", ")
    }

    var thresholdSummary: String? {
        guard let threshold else { return nil }
        let parts = threshold
            .filter { $0.value.shortDescription != "—" }  // Skip null values
            .map { "\($0.key): \($0.value.shortDescription)" }
            .sorted()
        return parts.isEmpty ? nil : parts.joined(separator: ", ")
    }
}

struct GoNoGoWarning: Decodable, Identifiable {
    var id: String { rawId ?? UUID().uuidString }
    var rawId: String?
    var label: String?
    var events: [GoNoGoWarningEvent]?

    enum CodingKeys: String, CodingKey {
        case rawId = "id", label, events
    }
}

struct GoNoGoWarningEvent: Decodable, Identifiable {
    var id: String { key ?? UUID().uuidString }
    var date: String?
    var kind: String?
    var title: String?
    var source: String?
    var severity: String?
    var importance: Int?
    var key: String?
}

/// Flexible JSON holder for value/threshold dictionaries
enum GoNoGoValue: Decodable {
    case null
    case string(String)
    case double(Double)
    case int(Int)
    case bool(Bool)
    case array([GoNoGoValue])
    case object([String: GoNoGoValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        // Handle null first
        if container.decodeNil() { self = .null; return }
        if let b = try? container.decode(Bool.self) { self = .bool(b); return }
        if let i = try? container.decode(Int.self) { self = .int(i); return }
        if let d = try? container.decode(Double.self) { self = .double(d); return }
        if let s = try? container.decode(String.self) { self = .string(s); return }
        if let arr = try? container.decode([GoNoGoValue].self) { self = .array(arr); return }
        if let obj = try? container.decode([String: GoNoGoValue].self) { self = .object(obj); return }
        // Instead of throwing, default to null for unknown types
        self = .null
    }

    var shortDescription: String {
        switch self {
        case .null:
            return "—"
        case .string(let s):
            return s
        case .double(let d):
            return String(format: "%.3g", d)
        case .int(let i):
            return "\(i)"
        case .bool(let b):
            return b ? "true" : "false"
        case .array(let arr):
            return "[\(arr.prefix(3).map { $0.shortDescription }.joined(separator: ", "))\(arr.count > 3 ? ", …" : "")]"
        case .object(let obj):
            let parts = obj.prefix(3).map { "\($0.key): \($0.value.shortDescription)" }
            let suffix = obj.count > 3 ? ", …" : ""
            return "{\(parts.joined(separator: ", "))\(suffix)}"
        }
    }
}

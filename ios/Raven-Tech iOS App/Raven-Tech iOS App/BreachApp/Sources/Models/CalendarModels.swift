import Foundation

struct CalendarResponse: Decodable {
    var view: String?
    var range: CalendarRange?
    var meta: CalendarMeta?
    @Default<Defaults.EmptyArray<[CalendarDay]>> var days: [CalendarDay]
}

struct CalendarRange: Decodable {
    var start: String?
    var end: String?
}

struct CalendarMeta: Decodable {
    var generatedAt: String?
    var engine1Only: Bool?
    var counts: CalendarCounts?
    var notes: [String]?
}

struct CalendarCounts: Decodable {
    var earningsRowsFetched: Int?
    var earningsRowsInRange: Int?
    var tickersSeen: Int?
    var tickersEligible: Int?
    var earningsSource: String?
    var earningsSnapshotKind: String?
    var snapshotEtDate: String?
    var universeMode: String?
    var universeSize: Int?
}

struct CalendarDay: Decodable, Identifiable {
    var id: String { date ?? UUID().uuidString }
    var date: String?
    var earnings: EarningsGroups?

    // Some backend event rows omit `date`. We normalize by inheriting the day's date
    // so SwiftUI IDs stay unique and stable.
    var events: [CalendarEvent]

    enum CodingKeys: String, CodingKey {
        case date, earnings, events
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.date = try c.decodeIfPresent(String.self, forKey: .date)
        self.earnings = try c.decodeIfPresent(EarningsGroups.self, forKey: .earnings)

        let decodedEvents = (try? c.decode([CalendarEvent].self, forKey: .events)) ?? []
        if let dayDate = self.date, !dayDate.isEmpty {
            self.events = decodedEvents.map { ev in
                var m = ev
                if m.date == nil || m.date?.isEmpty == true {
                    m.date = dayDate
                }
                return m
            }
        } else {
            self.events = decodedEvents
        }
    }
}

struct EarningsGroups: Decodable {
    @Default<Defaults.EmptyArray<[EarningsTicker]>> var bmo: [EarningsTicker]
    @Default<Defaults.EmptyArray<[EarningsTicker]>> var amc: [EarningsTicker]
    @Default<Defaults.EmptyArray<[EarningsTicker]>> var unk: [EarningsTicker]

    enum CodingKeys: String, CodingKey {
        case bmo = "BMO"
        case amc = "AMC"
        case unk = "UNK"
    }
}

struct EarningsTicker: Decodable, Identifiable {
    var id: String { ticker.uppercased() + "|" + (time ?? "") }
    var ticker: String
    var time: String?

    init(ticker: String) {
        self.ticker = ticker
    }

    enum CodingKeys: String, CodingKey {
        case ticker
        case symbol
        case time
    }

    init(from decoder: Decoder) throws {
        // Backend may send either:
        // - "AAPL"
        // - { "ticker": "AAPL", "time": "AMC" }
        if let s = try? decoder.singleValueContainer().decode(String.self) {
            self.ticker = s
            self.time = nil
            return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.ticker = (try? c.decode(String.self, forKey: .ticker))
            ?? (try? c.decode(String.self, forKey: .symbol))
            ?? ""
        self.time = try? c.decodeIfPresent(String.self, forKey: .time)
    }
}

struct CalendarEvent: Decodable, Identifiable {
    // Stable, unique ID for SwiftUI diffing.
    // Prefer real fields; fall back to a per-decode UUID if the payload is too sparse.
    private let localId: String
    var id: String {
        let parts: [String] = [
            key,
            kind,
            short,
            title,
            date,
            timeEt
        ]
        .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }

        let base = parts.joined(separator: "|")
        return base.isEmpty ? localId : base
    }

    var key: String?
    var kind: String?
    var title: String?
    var short: String?
    var date: String?
    var timeEt: String?
    var importance: Int?
    var playbook: Playbook?
    var forecast: Double?
    var previous: Double?
    var actual: Double?
    var unit: String?

    enum CodingKeys: String, CodingKey {
        case key, kind, title, short, date, timeEt, importance, playbook, forecast, previous, actual, unit
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.key = try c.decodeIfPresent(String.self, forKey: .key)
        self.kind = try c.decodeIfPresent(String.self, forKey: .kind)
        self.title = try c.decodeIfPresent(String.self, forKey: .title)
        self.short = try c.decodeIfPresent(String.self, forKey: .short)
        self.date = try c.decodeIfPresent(String.self, forKey: .date)
        self.timeEt = try c.decodeIfPresent(String.self, forKey: .timeEt)
        self.importance = try c.decodeIfPresent(Int.self, forKey: .importance)
        self.playbook = try c.decodeIfPresent(Playbook.self, forKey: .playbook)
        self.forecast = try c.decodeIfPresent(Double.self, forKey: .forecast)
        self.previous = try c.decodeIfPresent(Double.self, forKey: .previous)
        self.actual = try c.decodeIfPresent(Double.self, forKey: .actual)
        self.unit = try c.decodeIfPresent(String.self, forKey: .unit)

        self.localId = UUID().uuidString
    }
}

struct Playbook: Decodable {
    var deskView: [String]?
    var watch: [String]?
}

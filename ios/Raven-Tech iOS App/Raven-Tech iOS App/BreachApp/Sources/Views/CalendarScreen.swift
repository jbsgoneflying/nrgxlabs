import SwiftUI

struct CalendarScreen: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = CalendarViewModel()
    @State private var selectedEvent: CalendarEvent?

    var body: some View {
        NavigationStack {
            VStack {
                if viewModel.isLoading {
                    ProgressView("Loading calendar…")
                        .padding()
                }
                if let err = viewModel.error {
                    Text(err.localizedDescription).foregroundColor(.red).padding(.bottom, 4)
                }
                if let resp = viewModel.response {
                    scanStrip(resp)
                        .padding(.horizontal)
                    List {
                        ForEach(resp.days) { day in
                            CalendarDayRow(day: day, selectedEvent: $selectedEvent)
                        }
                    }
                } else {
                    Text("No calendar data yet.")
                        .foregroundColor(.secondary)
                        .padding()
                }
            }
            .navigationTitle("Calendar")
            .sheet(item: $selectedEvent) { ev in
                CalendarEventSheet(event: ev)
            }
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button {
                        Task { await viewModel.load(client: appState.apiClient) }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
            .task {
                await viewModel.load(client: appState.apiClient)
            }
        }
    }

    private func scanStrip(_ resp: CalendarResponse) -> some View {
        let totals = aggregateTotals(days: resp.days)
        let macroCounts = aggregateEvents(days: resp.days)
        return VStack(alignment: .leading, spacing: 12) {
            Text("Scan").font(.headline)
            HStack {
                scanCard(title: "Earnings", value: "\(totals.total)", detail: "BMO \(totals.bmo) · AMC \(totals.amc) · UNK \(totals.unk)")
                scanCard(title: "Macro", value: "\(macroCounts.hiMacro)", detail: "FED \(macroCounts.fed) · ECON \(macroCounts.econ)")
            }
        }
    }

    private func scanCard(title: String, value: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.subheadline).foregroundColor(.secondary)
            Text(value).font(.title2).fontWeight(.semibold).monospacedDigit()
            Text(detail).font(.caption).foregroundColor(.secondary)
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
    }

    private func aggregateTotals(days: [CalendarDay]) -> (total: Int, bmo: Int, amc: Int, unk: Int) {
        var bmo = 0, amc = 0, unk = 0
        for d in days {
            bmo += d.earnings?.bmo.count ?? 0
            amc += d.earnings?.amc.count ?? 0
            unk += d.earnings?.unk.count ?? 0
        }
        return (bmo + amc + unk, bmo, amc, unk)
    }

    private func aggregateEvents(days: [CalendarDay]) -> (hiMacro: Int, fed: Int, econ: Int) {
        var fed = 0, econ = 0
        for d in days {
            for ev in d.events {
                let kind = (ev.kind ?? "").uppercased()
                if kind == "FED" { fed += 1 }
                if kind == "ECON" { econ += 1 }
            }
        }
        return (fed + econ, fed, econ)
    }
}

private struct CalendarDayRow: View {
    let day: CalendarDay
    @Binding var selectedEvent: CalendarEvent?

    private var bmoCount: Int { day.earnings?.bmo.count ?? 0 }
    private var amcCount: Int { day.earnings?.amc.count ?? 0 }
    private var unkCount: Int { day.earnings?.unk.count ?? 0 }
    private var totalCount: Int { bmoCount + amcCount + unkCount }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(day.date ?? "—")
                    .font(.headline)
                Spacer()
                Text("\(totalCount)")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            if let e = day.earnings {
                EarningsGroupRow(label: "BMO", tickers: e.bmo)
                EarningsGroupRow(label: "AMC", tickers: e.amc)
                EarningsGroupRow(label: "UNK", tickers: e.unk)
            }
            if !day.events.isEmpty {
                Text("Events")
                    .font(.caption)
                    .foregroundColor(.secondary)
                ForEach(day.events) { ev in
                    Button {
                        selectedEvent = ev
                    } label: {
                        HStack {
                            Text(ev.short ?? ev.title ?? "Event")
                                .foregroundColor(.primary)
                            Spacer()
                            Text(ev.kind ?? "")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                }
            }
        }
        .padding(.vertical, 4)
    }
}

private struct EarningsGroupRow: View {
    let label: String
    let tickers: [EarningsTicker]

    var body: some View {
        let shown = tickers.filter { !$0.ticker.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        if shown.isEmpty { EmptyView() }
        HStack(spacing: 4) {
            Text(label)
                .font(.caption)
                .foregroundColor(.secondary)
                .frame(width: 40, alignment: .leading)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(shown) { tk in
                        Text(tk.ticker.uppercased())
                            .font(.subheadline)
                            .padding(.vertical, 4)
                            .padding(.horizontal, 8)
                            .background(RoundedRectangle(cornerRadius: 8).fill(Color(.tertiarySystemBackground)))
                    }
                }
            }
        }
    }
}

private struct CalendarEventSheet: View {
    let event: CalendarEvent

    var body: some View {
        NavigationStack {
            List {
                Section(header: Text("Details")) {
                    row("Title", event.title ?? event.short ?? "Event")
                    row("Kind", event.kind)
                    row("Date", event.date)
                    row("Time (ET)", event.timeEt)
                    row("Key", event.key)
                    if let imp = event.importance {
                        row("Importance", "\(imp)")
                    }
                }
                if let playbook = event.playbook {
                    Section(header: Text("Desk view")) {
                        ForEach(playbook.deskView ?? [], id: \.self, content: Text.init)
                    }
                    Section(header: Text("Watch")) {
                        ForEach(playbook.watch ?? [], id: \.self, content: Text.init)
                    }
                }
            }
            .navigationTitle(event.short ?? "Event")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func row(_ label: String, _ value: String?) -> some View {
        HStack {
            Text(label)
            Spacer()
            Text(value ?? "—").foregroundColor(.secondary)
        }
    }
}

import SwiftUI

struct EngineOneScreen: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = EngineOneViewModel()

    @State private var showingInfoSheet: InfoContent?
    @State private var showingGoNoGoSheet: Bool = false
    @State private var showingAllEvents: Bool = false

    var body: some View {
        NavigationStack {
            ZStack {
                backgroundGradient

                ScrollView {
                    VStack(spacing: 16) {
                        // Header with ticker input
                        EngineOneHeader(
                            ticker: $viewModel.ticker,
                            isLoading: viewModel.isLoading,
                            onRun: {
                                let generator = UIImpactFeedbackGenerator(style: .medium)
                                generator.impactOccurred()
                                Task { await viewModel.load(client: appState.apiClient) }
                            }
                        )
                        .padding(.horizontal)

                        // Error display
                        if let error = viewModel.error {
                            errorBanner(error)
                        }

                        // Results
                        if let response = viewModel.response {
                            resultsContent(response)
                        } else if !viewModel.isLoading && viewModel.error == nil {
                            emptyState
                        }
                    }
                    .padding(.bottom, 32)
                }
            }
            .navigationTitle("Engine 1")
            .navigationBarTitleDisplayMode(.inline)
            .sheet(item: $showingInfoSheet) { content in
                content.sheet()
            }
            .sheet(isPresented: $showingGoNoGoSheet) {
                GoNoGoBreakdownSheet(
                    decision: viewModel.response?.goNoGo,
                    ticker: viewModel.ticker.uppercased()
                )
            }
            .sheet(isPresented: $showingAllEvents) {
                allEventsSheet
            }
            .onAppear {
                // Check if navigated from Calendar with a pending ticker
                if let pending = appState.pendingTicker {
                    viewModel.ticker = pending
                    appState.pendingTicker = nil
                    Task { await viewModel.load(client: appState.apiClient) }
                }
            }
        }
    }

    @ViewBuilder
    private var allEventsSheet: some View {
        NavigationStack {
            List {
                if let events = viewModel.response?.events {
                    ForEach(events) { event in
                        BreachEventRow(event: event)
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    }
                }
            }
            .listStyle(.plain)
            .navigationTitle("All Events (\(viewModel.response?.events.count ?? 0))")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    // MARK: - Background

    private var backgroundGradient: some View {
        ZStack {
            Color(UIColor.systemBackground)

            RadialGradient(
                colors: [Color(hex: "007AFF").opacity(0.08), .clear],
                center: UnitPoint(x: 0.18, y: -0.10),
                startRadius: 0,
                endRadius: 700
            )

            RadialGradient(
                colors: [Color(hex: "34C759").opacity(0.05), .clear],
                center: UnitPoint(x: 0.85, y: 0.10),
                startRadius: 0,
                endRadius: 600
            )
        }
        .ignoresSafeArea()
    }

    // MARK: - Error Banner

    @ViewBuilder
    private func errorBanner(_ error: AppError) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.red)

            Text(error.localizedDescription)
                .font(.subheadline)
                .foregroundStyle(.primary)

            Spacer()
        }
        .padding(12)
        .background(Color.red.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.red.opacity(0.20), lineWidth: 1)
        )
        .padding(.horizontal)
    }

    // MARK: - Empty State

    private var emptyState: some View {
        VStack(spacing: 16) {
            Image(systemName: "chart.bar.doc.horizontal")
                .font(.system(size: 48))
                .foregroundStyle(.secondary.opacity(0.5))

            Text("Enter a ticker and tap Run")
                .font(.subheadline)
                .foregroundStyle(.secondary)

            Text("Analyze historical earnings breach probability,\nwing recommendations, and seasonality")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 60)
    }

    // MARK: - Results Content

    @ViewBuilder
    private func resultsContent(_ response: BreachResponse) -> some View {
        // Decision banner
        decisionBanner(response)
            .padding(.horizontal)

        // Regime section
        regimeSection(response.regime)
            .padding(.horizontal)

        // Summary metrics grid
        summaryMetricsGrid(response)
            .padding(.horizontal)

        // Wing recommendation
        wingRecommendationSection(response.wingRecommendation)
            .padding(.horizontal)

        // Quarter seasonality
        quarterSeasonalitySection(response.quarters)
            .padding(.horizontal)

        // Events history
        eventsSection(response.events)
            .padding(.horizontal)
    }

    // MARK: - Regime Section

    @ViewBuilder
    private func regimeSection(_ regime: RegimeData?) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Market Regime")
                .font(.caption)
                .fontWeight(.heavy)
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
                .padding(.leading, 4)

            MetricCardGrid(columns: 2) {
                MetricCard(
                    title: "Regime",
                    value: regime?.label ?? "—",
                    subtitle: regime?.guidance?.message ?? "Market environment label"
                )

                MetricCard(
                    title: "Trade Gate",
                    value: formatTradeGate(regime?.guidance?.tradeGate ?? regime?.tradeGate),
                    subtitle: tradeGateSubtitle(regime?.guidance?.tradeGate ?? regime?.tradeGate),
                    valueColor: tradeGateColor(regime?.guidance?.tradeGate ?? regime?.tradeGate)
                )

                MetricCard(
                    title: "Tail Multiplier",
                    value: formatMultiplier(regime?.tailMultiplier),
                    subtitle: "Wing width adjustment"
                )

                MetricCard(
                    title: "Regime Score",
                    value: formatScore(regime?.scores?.regimeScore),
                    subtitle: "Composite score (0-1)"
                )
            }
        }
    }

    private func formatTradeGate(_ gate: String?) -> String {
        guard let gate = gate else { return "—" }
        switch gate.uppercased() {
        case "OK": return "OK"
        case "CAUTION": return "CAUTION"
        case "NO_TRADE": return "NO TRADE"
        default: return gate
        }
    }

    private func tradeGateSubtitle(_ gate: String?) -> String {
        guard let gate = gate else { return "Trade permission" }
        switch gate.uppercased() {
        case "OK": return "Clear to trade"
        case "CAUTION": return "Proceed with care"
        case "NO_TRADE": return "Avoid trading"
        default: return "Trade permission"
        }
    }

    private func tradeGateColor(_ gate: String?) -> Color? {
        guard let gate = gate else { return nil }
        switch gate.uppercased() {
        case "OK": return Color(hex: "34C759")
        case "CAUTION": return .orange
        case "NO_TRADE": return Color(hex: "FF3B30")
        default: return nil
        }
    }

    private func formatMultiplier(_ value: Double?) -> String {
        guard let v = value else { return "—" }
        return String(format: "%.2f×", v)
    }

    private func formatScore(_ value: Double?) -> String {
        guard let v = value else { return "—" }
        return String(format: "%.2f", v)
    }

    // MARK: - Decision Banner

    @ViewBuilder
    private func decisionBanner(_ response: BreachResponse) -> some View {
        let isGo = computeIsGo(response)
        let bias = computeBias(response)

        DecisionBanner(
            ticker: viewModel.ticker.uppercased(),
            isGo: isGo,
            bias: bias,
            confidence: computeConfidence(response),
            chips: buildChips(response),
            asOfDate: nil,
            spot: nil,
            onGoNoGoTap: { showingGoNoGoSheet = true }
        )
    }

    private func computeIsGo(_ response: BreachResponse) -> Bool? {
        if let passed = response.goNoGo?.passed {
            return passed
        }
        guard let breachRate = response.summary?.breachRatePct else { return nil }
        // Simple heuristic: GO if breach rate < 30%
        return breachRate < 30
    }

    private func computeBias(_ response: BreachResponse) -> DecisionBanner.Bias? {
        guard let rec = response.wingRecommendation else { return nil }
        if let callMult = rec.callWingMultiple, let putMult = rec.putWingMultiple {
            if callMult > putMult + 0.2 { return .bearish }
            if putMult > callMult + 0.2 { return .bullish }
        }
        return .neutral
    }

    private func computeConfidence(_ response: BreachResponse) -> Int {
        guard let used = response.summary?.eventsUsed else { return 2 }
        if used >= 40 { return 5 }
        if used >= 30 { return 4 }
        if used >= 20 { return 3 }
        if used >= 10 { return 2 }
        return 1
    }

    private func buildChips(_ response: BreachResponse) -> [String] {
        var chips: [String] = []
        if let used = response.summary?.eventsUsed {
            chips.append("n=\(used)")
        }
        if let breachRate = response.summary?.breachRatePct {
            chips.append(String(format: "Breach %.1f%%", breachRate))
        }
        return chips
    }

    // MARK: - Summary Metrics

    @ViewBuilder
    private func summaryMetricsGrid(_ response: BreachResponse) -> some View {
        let summary = response.summary

        VStack(alignment: .leading, spacing: 10) {
            Text("Summary")
                .font(.caption)
                .fontWeight(.heavy)
                .foregroundStyle(.secondary)
                .textCase(.uppercase)
                .padding(.leading, 4)

            MetricCardGrid(columns: 2) {
                MetricCard(
                    title: "Breach Rate",
                    value: formatPct(summary?.breachRatePct),
                    subtitle: "Historical breach frequency",
                    onInfoTap: { showingInfoSheet = .breachRate }
                )

                MetricCard(
                    title: "Avg Overshoot",
                    value: formatPct(summary?.avgUpOvershootPct),
                    subtitle: "When breach occurs (upside)"
                )

                MetricCard(
                    title: "Realized Move",
                    value: formatPct(summary?.avgRealizedAllPct),
                    subtitle: "vs \(formatPct(summary?.avgImpliedAllPct)) implied"
                )

                MetricCard(
                    title: "Events Used",
                    value: "\(summary?.eventsUsed ?? 0)",
                    subtitle: "Historical sample size"
                )
            }
            
            // Expected Move section - shows both ORATS EM and calculated EM
            expectedMoveSection(response)
        }
    }
    
    // MARK: - Expected Move Section
    
    @ViewBuilder
    private func expectedMoveSection(_ response: BreachResponse) -> some View {
        let oratsEM = response.current?.impliedMovePct
        let calculatedEM = response.expectedMove?.expectedMovePct
        let nextEventEM = response.nextEvent?.impliedMovePctPlanned
        
        // Only show if we have at least one EM value
        if oratsEM != nil || calculatedEM != nil || nextEventEM != nil {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("Expected Move")
                        .font(.caption)
                        .fontWeight(.heavy)
                        .foregroundStyle(.secondary)
                        .textCase(.uppercase)
                    
                    Spacer()
                    
                    InfoButton { showingInfoSheet = .expectedMove }
                }
                .padding(.leading, 4)
                .padding(.top, 8)
                
                MetricCardGrid(columns: 2) {
                    // ORATS Implied Earnings Move (used for historical event calculations)
                    MetricCard(
                        title: "ORATS EM",
                        value: formatPct(oratsEM ?? nextEventEM),
                        subtitle: oratsEM != nil ? "Implied earnings move" : (nextEventEM != nil ? "Planned EM" : "—")
                    )
                    
                    // Calculated ATM-forward straddle EM
                    MetricCard(
                        title: "Straddle EM",
                        value: formatPct(calculatedEM),
                        subtitle: formatEMSubtitle(response.expectedMove)
                    )
                }
            }
        }
    }
    
    private func formatEMSubtitle(_ em: ExpectedMove?) -> String {
        guard let em = em else { return "ATM-forward method" }
        if let dte = em.dte {
            return "\(dte) DTE straddle"
        }
        return "ATM-forward method"
    }

    // MARK: - Wing Recommendation

    @ViewBuilder
    private func wingRecommendationSection(_ rec: WingRecommendation?) -> some View {
        GlassSurface {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Wing Recommendation")
                        .font(.caption)
                        .fontWeight(.heavy)
                        .foregroundStyle(.secondary)
                        .textCase(.uppercase)

                    Spacer()

                    InfoButton { showingInfoSheet = .wingRecommendation }
                }

                if let rec = rec {
                    Text(rec.recommendationLabel ?? "—")
                        .font(.title3)
                        .fontWeight(.bold)

                    HStack(spacing: 20) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Calls")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(String(format: "%.2fx", rec.callWingMultiple ?? 1.0))
                                .font(.headline)
                                .fontWeight(.bold)
                                .monospacedDigit()
                        }

                        VStack(alignment: .leading, spacing: 4) {
                            Text("Puts")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(String(format: "%.2fx", rec.putWingMultiple ?? 1.0))
                                .font(.headline)
                                .fontWeight(.bold)
                                .monospacedDigit()
                        }

                        Spacer()

                        if let gate = rec.tradeGate {
                            Pill(
                                text: gate,
                                style: gate.lowercased().contains("go") ? .good : .warn
                            )
                        }
                    }

                    if let rationale = rec.rationale {
                        Text(rationale)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(3)
                    }
                } else {
                    Text("No recommendation available")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - Quarter Seasonality

    @ViewBuilder
    private func quarterSeasonalitySection(_ quarters: [String: QuarterStats]?) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Quarter Seasonality")
                    .font(.caption)
                    .fontWeight(.heavy)
                    .foregroundStyle(.secondary)
                    .textCase(.uppercase)

                Spacer()

                InfoButton { showingInfoSheet = .quarterSeasonality }
            }
            .padding(.leading, 4)

            if let quarters = quarters, !quarters.isEmpty {
                LazyVGrid(
                    columns: [
                        GridItem(.flexible(), spacing: 10),
                        GridItem(.flexible(), spacing: 10)
                    ],
                    spacing: 10
                ) {
                    ForEach(quarters.keys.sorted(), id: \.self) { key in
                        let stats = quarters[key]
                        QuarterSeasonalityCard(
                            quarter: key,
                            breachRate: stats?.breachRatePct,
                            recommendation: stats?.recommendation,
                            isCurrentQuarter: isCurrentQuarter(key)
                        )
                    }
                }
            } else {
                Text("No quarter data available")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding()
            }
        }
    }

    private func isCurrentQuarter(_ quarter: String) -> Bool {
        let month = Calendar.current.component(.month, from: Date())
        let currentQ: String
        switch month {
        case 1...3: currentQ = "Q1"
        case 4...6: currentQ = "Q2"
        case 7...9: currentQ = "Q3"
        default: currentQ = "Q4"
        }
        return quarter == currentQ
    }

    // MARK: - Events Section

    @ViewBuilder
    private func eventsSection(_ events: [BreachEvent]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Earnings History")
                    .font(.caption)
                    .fontWeight(.heavy)
                    .foregroundStyle(.secondary)
                    .textCase(.uppercase)

                Spacer()

                Text("\(events.count) events")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            .padding(.leading, 4)

            if events.isEmpty {
                Text("No events")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding()
            } else {
                GlassSurface(padding: 0) {
                    VStack(spacing: 0) {
                        ForEach(events.prefix(10)) { event in
                            BreachEventRow(event: event)

                            if event.id != events.prefix(10).last?.id {
                                Divider()
                                    .padding(.horizontal, 12)
                            }
                        }

                        if events.count > 10 {
                            Divider()
                                .padding(.horizontal, 12)

                            Button {
                                showingAllEvents = true
                            } label: {
                                HStack {
                                    Text("+\(events.count - 10) more events")
                                        .font(.caption)
                                        .fontWeight(.medium)
                                    Image(systemName: "chevron.right")
                                        .font(.caption2)
                                }
                                .foregroundStyle(.secondary)
                                .padding(12)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
    }

    // MARK: - Helpers

    private func formatPct(_ value: Double?) -> String {
        guard let v = value else { return "—" }
        return String(format: "%.2f%%", v)
    }
}

// MARK: - InfoContent Identifiable

extension InfoContent: Identifiable {
    var id: String { title }
}

// MARK: - Preview

struct EngineOneScreen_Previews: PreviewProvider {
    static var previews: some View {
        EngineOneScreen()
            .environmentObject(AppState())
    }
}

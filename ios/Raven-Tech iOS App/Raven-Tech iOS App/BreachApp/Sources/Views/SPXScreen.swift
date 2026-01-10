import SwiftUI

struct SPXScreen: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var viewModel = SPXViewModel()

    var body: some View {
        NavigationStack {
            Form {
                Section(header: Text("Engine 2 Controls")) {
                    Picker("Underlying", selection: $viewModel.underlying) {
                        Text("SPX").tag("SPX")
                        Text("SPY").tag("SPY")
                        Text("QQQ").tag("QQQ")
                    }
                    Picker("Entry day", selection: $viewModel.entryDay) {
                        Text("Mon").tag("mon")
                        Text("Tue").tag("tue")
                        Text("Wed").tag("wed")
                    }
                    Picker("Seasonality", selection: $viewModel.seasonalityMode) {
                        Text("None").tag("none")
                        Text("Quarter").tag("quarter")
                        Text("Month").tag("month")
                        Text("Summer").tag("summer")
                        Text("OpEx").tag("opex")
                    }

                    Button {
                        Task { await viewModel.load(client: appState.apiClient) }
                    } label: {
                        viewModel.isLoading ? AnyView(ProgressView()) : AnyView(Text("Run"))
                    }
                    .disabled(viewModel.isLoading)

                    if let enabled = viewModel.flags?.enableEngine2SpxIc, enabled == false {
                        Text("Engine 2 disabled on server (ENABLE_ENGINE2_SPX_IC=0).")
                            .foregroundColor(.red)
                    }
                }

                if let err = viewModel.error {
                    Section {
                        Text(err.localizedDescription).foregroundColor(.red)
                    }
                }

                Section(header: Text("SPX IC")) {
                    Text(viewModel.icSummary).foregroundColor(.secondary)
                    if let ic = viewModel.ic {
                        row("Underlying", ic.underlying?.symbol)
                        row("As of", ic.asOfDate)
                        row("Regime", ic.current?.regime?.bucket)
                        row("Regime score", ic.current?.regime?.score100.map { String(format: "%.1f / 100", $0) })
                        row("Macro multiplier", ic.current?.macro?.multiplier.map { String(format: "%.2fx", $0) })
                        row("High-impact events", ic.current?.macro?.highImpactUS?.count.map { String($0) })
                    }
                }

                Section(header: Text("SPX Levels")) {
                    Text(viewModel.levelsSummary).foregroundColor(.secondary)
                    if let lv = viewModel.levels?.levels {
                        row("Symbol used", lv.symbolUsed)
                        row("View", lv.view)
                        row("Expiry", lv.expiry)
                        row("Spot", lv.spot.map { String(format: "%.2f", $0) })
                        row("Gamma flip", lv.gammaFlipStrike.map { String(format: "%.0f", $0) })
                        row("Heatmap stability", lv.gexHeatmap?.stability?.label)
                    }
                }
            }
            .navigationTitle("SPX")
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

struct SPXScreen_Previews: PreviewProvider {
    static var previews: some View {
        SPXScreen().environmentObject(AppState())
    }
}

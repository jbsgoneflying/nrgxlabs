import SwiftUI

/// Mobile-friendly replacement for web's hover tooltips
struct InfoSheet: View {
    let title: String
    let bodyText: String
    var bullets: [String]?
    var deskView: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(bodyText)
                        .font(.subheadline)
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)

                    if let bullets = bullets, !bullets.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(bullets, id: \.self) { bullet in
                                HStack(alignment: .top, spacing: 10) {
                                    Circle()
                                        .fill(RavenTheme.accentBlue.opacity(0.18))
                                        .frame(width: 8, height: 8)
                                        .overlay(
                                            Circle()
                                                .stroke(RavenTheme.accentBlue.opacity(0.22), lineWidth: 1)
                                        )
                                        .padding(.top, 5)

                                    Text(bullet)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                    }

                    if let desk = deskView {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Desk View")
                                .font(.caption)
                                .fontWeight(.heavy)
                                .foregroundStyle(.secondary)
                                .textCase(.uppercase)

                            Text(desk)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.top, 8)
                    }
                }
                .padding()
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium])
        .presentationDragIndicator(.visible)
    }
}

/// Info content definitions for various metrics
enum InfoContent {
    case breachRate
    case avgOvershoot
    case wingRecommendation
    case regimeScore
    case macroMultiplier
    case dealerGamma
    case gammaFlip
    case vwap
    case historicalOdds
    case quarterSeasonality
    case expectedMove
    case custom(title: String, body: String, bullets: [String]?, deskView: String?)

    var title: String {
        switch self {
        case .breachRate: return "Breach Rate"
        case .avgOvershoot: return "Average Overshoot"
        case .wingRecommendation: return "Wing Recommendation"
        case .regimeScore: return "Regime Score"
        case .macroMultiplier: return "Macro Multiplier"
        case .dealerGamma: return "Dealer Gamma"
        case .gammaFlip: return "Gamma Flip"
        case .vwap: return "VWAP"
        case .historicalOdds: return "Historical Odds"
        case .quarterSeasonality: return "Quarter Seasonality"
        case .expectedMove: return "Expected Move"
        case .custom(let title, _, _, _): return title
        }
    }

    var bodyText: String {
        switch self {
        case .breachRate:
            return "The percentage of historical earnings events where the realized move exceeded the implied move (breached the expected range)."
        case .avgOvershoot:
            return "When a breach occurs, this shows how much the realized move typically exceeds the implied move on average."
        case .wingRecommendation:
            return "Suggested wing width multipliers for call and put sides based on historical skew and directional bias."
        case .regimeScore:
            return "A composite score (0-100) measuring current market conditions across trend, volatility, stress, events, and dispersion."
        case .macroMultiplier:
            return "A multiplier applied to expected move calculations based on upcoming macro events (CPI, FOMC, NFP, etc.)."
        case .dealerGamma:
            return "Net dealer gamma exposure across strikes. Positive gamma = dealers hedge by selling rallies/buying dips. Negative = dealers amplify moves."
        case .gammaFlip:
            return "The strike level where dealer gamma exposure flips from positive to negative (or vice versa). Key inflection point for price behavior."
        case .vwap:
            return "Volume-Weighted Average Price from the most recent trading session. Used as a key reference level."
        case .historicalOdds:
            return "Historical breach frequencies across different width multiples, conditioned on regime and macro bucket."
        case .quarterSeasonality:
            return "Breach rates broken down by fiscal quarter, showing seasonal patterns in earnings behavior."
        case .expectedMove:
            return "The expected price move over earnings, shown from two sources: ORATS EM (implied earnings move from ORATS data) and Straddle EM (calculated from ATM-forward straddle pricing)."
        case .custom(_, let body, _, _):
            return body
        }
    }

    var bullets: [String]? {
        switch self {
        case .breachRate:
            return [
                "Higher breach rate = more volatile earnings reactions",
                "Consider wider wings when breach rate is elevated",
                "Compare to market average (~22-25%)"
            ]
        case .regimeScore:
            return [
                "0-25: Low volatility, trending market",
                "25-50: Normal conditions",
                "50-75: Elevated volatility or stress",
                "75-100: High stress, event-driven"
            ]
        case .dealerGamma:
            return [
                "Positive gamma: stabilizing, mean-reverting",
                "Negative gamma: amplifying, trending",
                "Magnitude matters: larger = stronger effect"
            ]
        case .expectedMove:
            return [
                "ORATS EM: Implied earnings move from ORATS cores data — used for historical breach calculations",
                "Straddle EM: Calculated from ATM-forward straddle pricing on the option chain",
                "Both values should be similar; large divergence may indicate unusual market conditions"
            ]
        case .custom(_, _, let bullets, _):
            return bullets
        default:
            return nil
        }
    }

    var deskView: String? {
        switch self {
        case .breachRate:
            return "Use this to gauge position sizing. Higher breach rates warrant smaller positions or wider structures."
        case .wingRecommendation:
            return "These multipliers are suggestions based on historical data. Always consider current IV levels and your risk tolerance."
        case .expectedMove:
            return "ORATS EM is the figure used for historical earnings analysis and breach calculations. Straddle EM is a live calculation that may differ slightly due to market conditions."
        case .custom(_, _, _, let desk):
            return desk
        default:
            return nil
        }
    }

    func sheet() -> InfoSheet {
        InfoSheet(
            title: title,
            bodyText: bodyText,
            bullets: bullets,
            deskView: deskView
        )
    }
}

#Preview {
    VStack {
        Text("Tap to show sheet")
    }
    .sheet(isPresented: .constant(true)) {
        InfoContent.breachRate.sheet()
    }
}

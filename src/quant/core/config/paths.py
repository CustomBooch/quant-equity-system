from __future__ import annotations

from quant.core.paths import REPO

ROOT = REPO                                                         # data under record/, config at the checkout (quant.core.paths)
CONFIG_DIR = ROOT / "config"

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
CLEANED_DIR = DATA_DIR / "cleaned"
ANALYSIS_DIR = DATA_DIR / "analysis"
REPORTS_DIR = DATA_DIR / "reports"
METADATA_DIR = DATA_DIR / "metadata"
REGISTRY_DIR = DATA_DIR / "registry"
SPLITS_DIR = DATA_DIR / "splits"
PROFILING_RESULTS_DIR = ANALYSIS_DIR / "profiling"
MODEL_ANALYSIS_DIR = ANALYSIS_DIR / "model"
FRED_ANALYSIS_DIR = ANALYSIS_DIR / "macro"
MACRO_ANALYSIS_DIR = FRED_ANALYSIS_DIR

CRSP_RAW_DIR = RAW_DIR / "CRSP"
COMPUSTAT_RAW_DIR = RAW_DIR / "Compustat"
LSEG_RAW_DIR = RAW_DIR / "LSEG"
IBES_RAW_DIR = LSEG_RAW_DIR / "IBES"
FISD_RAW_DIR = LSEG_RAW_DIR / "FISD"
BVD_RAW_DIR = RAW_DIR / "Bureau van Dijk (Moody's)"
MSCI_RAW_DIR = RAW_DIR / "MSCI (ESG)"
TREASURY_RAW_DIR = RAW_DIR / "Treasury"
FRED_RAW_DIR = RAW_DIR / "FRED"
UNCERTAINTY_RAW_DIR = RAW_DIR / "Macro and Financial Uncertainty"
EIA_RAW_DIR = RAW_DIR / "EIA"
SEC_RAW_DIR = RAW_DIR / "SEC"
WORLD_BANK_RAW_DIR  = RAW_DIR / "World Bank"
PINK_SHEET_PATH     = WORLD_BANK_RAW_DIR / "CMO-Historical-Data-Monthly.xlsx"


THOMSON_REUTERS_13F_RAW_DIR = RAW_DIR / "Thomson Reuters Institutional (13f) Holdings"
THOMSON_REUTERS_13F_MASTER_RAW = THOMSON_REUTERS_13F_RAW_DIR / "13f_master.csv"
THOMSON_REUTERS_13F_TYPE1_RAW = THOMSON_REUTERS_13F_RAW_DIR / "13f_type1.csv"
THOMSON_REUTERS_13F_CLEANED_PARQUET = CLEANED_DIR / "13f_holdings.parquet"
THOMSON_REUTERS_13F_MANAGER_DIMENSION_PARQUET = CLEANED_DIR / "13f_manager_history.parquet"
INSTITUTIONAL_OWNERSHIP_PARQUET = CLEANED_DIR / "institutional_ownership.parquet"
INSTITUTIONAL_OWNERSHIP_CSV = CLEANED_DIR / "institutional_ownership.csv"
THOMSON_REUTERS_13F_TYPE3_RAW = THOMSON_REUTERS_13F_RAW_DIR / "13f_type3.csv"
THOMSON_REUTERS_13F_TYPE4_RAW = THOMSON_REUTERS_13F_RAW_DIR / "13f_type4.csv"
EIA_FEATURES_PARQUET = CLEANED_DIR / "eia_features.parquet"
EIA_FEATURES_CSV     = CLEANED_DIR / "eia_features.csv"

# Legacy CSV aliases retained for compatibility with older tooling; the canonical
# production outputs are the parquet files defined immediately above.
THOMSON_REUTERS_13F_CLEANED_PATH = CLEANED_DIR / "thomson_reuters_13f.csv"
THOMSON_REUTERS_13F_MANAGER_DIMENSION_PATH = CLEANED_DIR / "thomson_reuters_13f_manager_dimension.csv"

RISK_FACTORS_RAW_DIR = RAW_DIR / "Risk Factors"
MACRO_FINANCE_SOCIETY_RAW_DIR = RAW_DIR / "Macro Finance Society"

SECURITY_MASTER_SOURCE_RAW = COMPUSTAT_RAW_DIR / "ccm_link.csv"
SECURITY_MASTER_CLEANED_PATH = CLEANED_DIR / "security_master.csv"
SECURITY_MASTER_CLEANED_PARQUET = CLEANED_DIR / "security_master.parquet"
SECURITY_MASTER_PATH = CLEANED_DIR / "security_master.parquet"
SECURITY_MASTER_CSV = CLEANED_DIR / "security_master.csv"

CRSP_DAILY_RAW = CRSP_RAW_DIR / "crsp.csv"
COMPUSTAT_RAW = COMPUSTAT_RAW_DIR / "compustat.csv"
COMPUSTAT_QUARTERLY_RAW = COMPUSTAT_RAW_DIR / "compustat_quarterly.csv"
CCM_LINK_RAW = COMPUSTAT_RAW_DIR / "ccm_link.csv"
IDENTIFIER_MASTER_RAW = COMPUSTAT_RAW_DIR / "identifier_master.csv"
CORPORATE_SUBSIDIARY_RELATIONSHIP_RAW = COMPUSTAT_RAW_DIR / "corporate_subsidiary_relationship.csv"

IBES_RAW = IBES_RAW_DIR / "ibes.csv"
MSCI_ESG_RAW = MSCI_RAW_DIR / "msci_esg.csv"
BVD_COMPANY_PROFILES_RAW = BVD_RAW_DIR / "company_profiles.csv"
BVD_SHAREHOLDERS_RAW = BVD_RAW_DIR / "current_shareholders_first_level.csv"
BVD_SUBSIDIARIES_RAW = BVD_RAW_DIR / "subsidiaries_first_level.csv"
RISK_FACTORS_5FF_DAILY_RAW = RISK_FACTORS_RAW_DIR / "5ff_daily.csv"
RISK_FACTORS_5FF_MONTHLY_RAW = RISK_FACTORS_RAW_DIR / "5ff_monthly.csv"
RISK_FACTORS_PASTOR_STAMBAUGH_RAW = RISK_FACTORS_RAW_DIR / "pastor-stambaugh.csv"
MACRO_FINANCE_SOCIETY_Q5_FACTORS_DAILY_RAW = MACRO_FINANCE_SOCIETY_RAW_DIR / "q5_factors_daily_2024.csv"

FISD_RATINGS_RAW = FISD_RAW_DIR / "ratings.csv"
FISD_ISSUERS_RAW = FISD_RAW_DIR / "issuers.csv"
FISD_ISSUES_RAW = FISD_RAW_DIR / "issues.csv"
FISD_ISSUERS_PARQUET = CLEANED_DIR / "fisd_issuers.parquet"
FISD_ISSUES_PARQUET = CLEANED_DIR / "fisd_issues.parquet"
FISD_RATINGS_PARQUET = CLEANED_DIR / "fisd_ratings.parquet"
FISD_LINKED_PARQUET = CLEANED_DIR / "fisd_linked.parquet"
FISD_FEATURES_PARQUET = CLEANED_DIR / "fisd_features.parquet"
FISD_FEATURES_CSV = CLEANED_DIR / "fisd_features.csv"
FISD_ISSUERS_CLEANED_PATH = FISD_ISSUERS_PARQUET
FISD_ISSUES_CLEANED_PATH = FISD_ISSUES_PARQUET
FISD_RATINGS_CLEANED_PATH = FISD_RATINGS_PARQUET
FISD_LINKED_PATH = FISD_LINKED_PARQUET
FISD_FEATURES_PATH = FISD_FEATURES_PARQUET

MODEL_DATASET_PATH = CLEANED_DIR / "model_dataset.parquet"
FEATURE_MATRIX_PATH = CLEANED_DIR / "feature_matrix.csv"
MACRO_MONTHLY_PATH = CLEANED_DIR / "macro_monthly.parquet"
MODEL_DATASET_NAME = "model_dataset"
FEATURE_MATRIX_NAME = "feature_matrix"
MACRO_MONTHLY_NAME = "macro_monthly"

# Historical compatibility aliases. These are not separate physical files.
MODEL_DATASET_PARQUET = MODEL_DATASET_PATH
MODEL_DATASET_CSV = CLEANED_DIR / "model_dataset.csv"
FEATURE_MATRIX_CSV = FEATURE_MATRIX_PATH
FEATURE_MATRIX_PARQUET = CLEANED_DIR / "feature_matrix.parquet"
MACRO_MONTHLY_PARQUET = MACRO_MONTHLY_PATH
MACRO_MONTHLY_CSV = CLEANED_DIR / "macro_monthly.csv"
MACRO_MONTHLY_CURATED_PATH = CLEANED_DIR / "macro_monthly_curated.csv"
MACRO_MONTHLY_DECORRELATED_PATH = CLEANED_DIR / "macro_monthly_decorrelated.csv"

SEC_EVENT_FEATURES_PARQUET = CLEANED_DIR / "sec_event_features.parquet"

CRSP_MONTHLY_PATH = CLEANED_DIR / "crsp_monthly.csv"
CRSP_MOMENTUM_PATH = CLEANED_DIR / "crsp_momentum.csv"
CRSP_VOLATILITY_PATH = CLEANED_DIR / "crsp_volatility.csv"
CRSP_WML_FACTOR_PATH = CLEANED_DIR / "crsp_wml_factor.csv"
CRSP_SIZE_PATH = CLEANED_DIR / "crsp_size.csv"
CRSP_MOMENTUM_SIZE_PATH = CLEANED_DIR / "crsp_momentum_size.csv"
CRSP_MOMENTUM_SIZE_BUCKETED_PATH = CLEANED_DIR / "crsp_momentum_size_bucketed.csv"
CRSP_MOMENTUM_SIZE_PORTFOLIOS_PATH = CLEANED_DIR / "crsp_momentum_size_portfolios.csv"

COMPUSTAT_CLEANED_PATH = CLEANED_DIR / "compustat_cleaned.csv"
CCM_LINK_CLEANED_PATH = CLEANED_DIR / "ccm_link_cleaned.csv"
COMPUSTAT_CRSP_JOINED_PATH = CLEANED_DIR / "compustat_crsp_joined.csv"
COMPUSTAT_FUNDAMENTALS_PATH = CLEANED_DIR / "compustat_fundamentals.csv"
COMPUSTAT_LINKED_PATH = CLEANED_DIR / "compustat_linked.csv"
COMPUSTAT_QUARTERLY_CLEANED_PATH = CLEANED_DIR / "compustat_quarterly_cleaned.csv"

WALK_FORWARD_FULL_HISTORY_PATH = CLEANED_DIR / "walk_forward_full_history.csv"
WALK_FORWARD_PREDICTIONS_PATH = CLEANED_DIR / "walk_forward_predictions.csv"
MODEL_PREDICTIONS_PATH = CLEANED_DIR / "model_predictions.csv"
DECILE_RETURNS_PATH = CLEANED_DIR / "decile_returns.csv"
ROLLING_IC_PATH = CLEANED_DIR / "rolling_ic.csv"
BULL_BEAR_REGIME_PATH = CLEANED_DIR / "bull_bear_regime.csv"

IBES_CLEANED_PATH = CLEANED_DIR / "ibes_cleaned.csv"
IBES_MONTHLY_PATH = CLEANED_DIR / "ibes_monthly.csv"
IBES_FEATURES_PATH = CLEANED_DIR / "ibes_features.csv"
IBES_CURATED_PATH = CLEANED_DIR / "ibes_curated.csv"

BVD_COMPANY_PROFILES_PATH = CLEANED_DIR / "bvd_company_profiles.csv"
BVD_SHAREHOLDERS_FINAL_PATH = CLEANED_DIR / "bvd_shareholders_final.csv"
BVD_SUBSIDIARIES_FINAL_PATH = CLEANED_DIR / "bvd_subsidiaries_final.csv"

MSCI_ESG_CLEANED_PATH = CLEANED_DIR / "msci_esg_cleaned.csv"
MSCI_ISSUER_MASTER_PATH = CLEANED_DIR / "msci_issuer_master.parquet"
MSCI_ISSUER_MASTER_CSV = CLEANED_DIR / "msci_issuer_master.csv"
MSCI_ISSUER_HISTORY_PATH = CLEANED_DIR / "msci_issuer_history.parquet"
MSCI_ISSUER_HISTORY_CSV = CLEANED_DIR / "msci_issuer_history.csv"
MSCI_MONTHLY_PANEL_PATH = CLEANED_DIR / "msci_monthly_panel.parquet"
MSCI_MONTHLY_PANEL_CSV = CLEANED_DIR / "msci_monthly_panel.csv"
MSCI_FEATURES_PATH = CLEANED_DIR / "msci_features.parquet"
MSCI_FEATURES_CSV = CLEANED_DIR / "msci_features.csv"
MSCI_LINKED_PATH = CLEANED_DIR / "msci_linked.parquet"
MSCI_LINKED_CSV = CLEANED_DIR / "msci_linked.csv"
MSCI_PERMNO_HISTORY_PATH = CLEANED_DIR / "msci_permno_history.parquet"
MSCI_PERMNO_HISTORY_CSV = CLEANED_DIR / "msci_permno_history.csv"

RATINGS_CLEANED_PATH = CLEANED_DIR / "ratings_cleaned.csv"

TREASURY_REAL_LONG_TERM_RATES_COMBINED_PATH = CLEANED_DIR / "treasury_real_long_term_rates_combined.csv"
TREASURY_BILL_RATES_COMBINED_PATH = CLEANED_DIR / "treasury_bill_rates_combined.csv"
TREASURY_LONG_TERM_RATES_COMBINED_PATH = CLEANED_DIR / "treasury_long_term_rates_combined.csv"
TREASURY_PAR_REAL_YIELD_CURVE_RATES_COMBINED_PATH = CLEANED_DIR / "treasury_par_real_yield_curve_rates_combined.csv"
TREASURY_PAR_YIELD_CURVE_RATES_COMBINED_PATH = CLEANED_DIR / "treasury_par_yield_curve_rates_combined.csv"

DATASETS_REGISTRY_FILE = CONFIG_DIR / "datasets.yml"
IDENTIFIER_COLUMNS_FILE = CONFIG_DIR / "identifier_columns.json"


def ensure_directories() -> None:
    for path in (
        DATA_DIR,
        RAW_DIR,
        CLEANED_DIR,
        ANALYSIS_DIR,
        REPORTS_DIR,
        METADATA_DIR,
        REGISTRY_DIR,
        SPLITS_DIR,
        PROFILING_RESULTS_DIR,
        MODEL_DATASET_PATH.parent,
        FEATURE_MATRIX_PATH.parent,
        MACRO_MONTHLY_PATH.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)

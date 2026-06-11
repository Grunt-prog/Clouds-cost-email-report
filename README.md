# ☁️ Cloud Cost Report Automate

A Python-based automation tool that collects month-to-date cloud spend across **AWS**, **Azure**, and **GCP**, generates a consolidated HTML cost report with forecasts, and delivers it via email using the **Microsoft Graph API**.

---

## 📁 Project Structure

```
CLOUD-REPORT-AUTOMATE/
├── config.py                          # All credentials and configuration
├── main.py                            # Entry point — fetches costs, builds HTML, sends email
├── insights-318308-bce1cf181445.json  # GCP Service Account key (keep secret, never commit)
├── requirements.txt                   # Python dependencies
├── .gitignore                         # Excludes secrets and virtualenv
├── myenv/                             # Python virtual environment (not committed)
└── README.md                          # This file
```

---

## ⚙️ How It Works

```
main.py
  ├── fetch_aws_cost()        → AWS Cost Explorer API   (boto3)
  ├── fetch_aws_forecast()    → AWS Cost Explorer API   (get_cost_forecast)
  ├── fetch_azure_cost()      → Azure Cost Management API
  ├── fetch_azure_forecast()  → Azure Cost Management Forecast API
  ├── fetch_gcp_cost()        → GCP BigQuery billing export
  ├── fetch_gcp_forecast()    → GCP Cloud Billing Budgets API
  ├── build_html()            → Consolidated HTML email
  └── send_email()            → Microsoft Graph API (sendMail)
```

- All costs are converted from billing currency (INR) to **USD** using a live exchange rate fetched from [open.er-api.com](https://open.er-api.com).
- GCP data shows the actual latest date available in BigQuery (accounts for BQ export lag of ~2 days).
- Forecasts use direct cloud APIs (AWS CE / Azure Forecast / GCP Budgets) and fall back to linear projection if unavailable.

---

## 🔧 Prerequisites

- Python 3.10+
- A virtual environment (recommended)
- Cloud credentials for AWS, Azure, and GCP
- A Microsoft 365 mailbox with Graph API access

---

## 📦 Installation

```bash
# 1. Clone the repo
git clone <your-repo-url>
cd CLOUD-REPORT-AUTOMATE

# 2. Create and activate virtual environment
python -m venv myenv
source myenv/bin/activate        # Linux/macOS
myenv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

---

## 🔑 Configuration

Edit `config.py` and fill in all values:

```python
# ── Microsoft Graph API (email delivery) ──────────────────────────
TENANT_ID      = "your-azure-tenant-id"
CLIENT_ID      = "your-app-client-id"
CLIENT_SECRET  = "your-app-client-secret"
FROM_EMAIL     = "sender@yourdomain.com"
TO_EMAIL       = ["recipient1@domain.com", "recipient2@domain.com"]

# ── AWS ───────────────────────────────────────────────────────────
AWS_ACCESS_KEY = "AKIA..."
AWS_SECRET_KEY = "your-secret"
AWS_REGION     = "us-east-1"

# ── Azure ─────────────────────────────────────────────────────────
AZURE_SUBSCRIPTION_ID = "your-subscription-id"
AZURE_TENANT_ID       = "your-azure-tenant-id"
AZURE_CLIENT_ID       = "your-client-id"
AZURE_CLIENT_SECRET   = "your-client-secret"

# ── GCP ───────────────────────────────────────────────────────────
GCP_DATASET_TABLE      = "project.dataset.gcp_billing_export_resource_v1_XXXXXX"
GCP_SERVICE_ACCOUNT_JSON = "Your_service_account.json"
GCP_BILLING_ACCOUNT_ID = "XXXXXX-XXXXXX-XXXXXX"  
GCP_PROJECT_ID         = "Your Project ID"

# ── Report settings ───────────────────────────────────────────────
REPORT_DAY  = 28        # Day of month to cap the report end date
EMAIL_NOTE  = ""        # Optional note appended at the bottom of the email
```

---

## ☁️ Cloud Permissions Required

### AWS
| Permission | Why |
|---|---|
| `ce:GetCostAndUsage` | Fetch MTD actual costs |
| `ce:GetCostForecast` | Fetch month-end forecast |

### Azure
| Permission | Why |
|---|---|
| `Cost Management Reader` on subscription | Fetch actual + forecast costs |

### GCP
| Permission | Why |
|---|---|
| `roles/bigquery.dataViewer` on dataset | Query billing export table |
| `roles/bigquery.jobUser` on project | Run BQ queries |
| `roles/billing.viewer` on billing account | Read Budgets API for forecast |

### Microsoft Graph API (email)
The registered App in Azure AD needs:
- `Mail.Send` — application permission (not delegated)

---

## ▶️ Usage

```bash
# Activate virtual environment first
source myenv/bin/activate

# Run the report
python main.py
```

**Sample console output:**
```
📅 Reporting period : 2026-06-01 → 2026-06-12  (REPORT_DAY=28)
  💱 Live INR→USD rate: 1 INR = 0.011976 USD  (1 USD = 83.50 INR)

  → AWS (MTD actual) ...
     AWS MTD total : $325.25
  → AWS forecast ...
     AWS forecast (CE API): $725.23

  → Azure (MTD actual) ...
     Azure MTD raw total (INR): 17,432.10
     Azure MTD total (USD) : $208.97
  → Azure forecast ...
     Azure forecast (USD) : $612.19

  → GCP (MTD actual) ...
     GCP BQ export — actual data available up to: 2026-06-09
     GCP MTD total (USD) : $26.23
  → GCP forecast (Budgets API) ...
     GCP forecast (USD) : $71.54

  → Building HTML ...
  → Sending email ...
✅ Token fetched
✅ Email sent to ['recipient@domain.com']

🎉 Done!
```

---

## ⏰ Scheduling

### Linux — Cron (daily at 8:00 AM IST = 2:30 AM UTC)
```bash
crontab -e

# Add this line:
30 2 * * * /path/to/myenv/bin/python /path/to/main.py >> /var/log/cloud_cost_report.log 2>&1
```

### Windows — Task Scheduler
```
Action  : Start a program
Program : C:\path\to\myenv\Scripts\python.exe
Arguments: C:\path\to\main.py
Trigger : Daily at 08:00 AM
```

---

## 🔒 Security Notes

- **Never commit** `config.py` or the GCP Service Account JSON to version control.
- Both are already listed in `.gitignore`.
- Rotate credentials regularly and use least-privilege IAM roles.
- Consider storing secrets in environment variables or a secrets manager (AWS Secrets Manager / Azure Key Vault / GCP Secret Manager) for production use.

---

## 📋 Requirements

Key packages (`requirements.txt`):

```
boto3
azure-identity
azure-mgmt-costmanagement
google-cloud-bigquery
google-cloud-billing-budgets
google-auth
requests
```

---

## 🐛 Troubleshooting

| Issue | Fix |
|---|---|
| `GCP query returned 0 rows` | BQ export is still backfilling — wait 2–5 days after enabling |
| `Azure 429 rate limit` | Script auto-retries with exponential backoff (up to 4 attempts) |
| `AWS forecast API failed` | Falls back to linear estimate; check IAM has `ce:GetCostForecast` |
| `GCP forecast: no budgets found` | Create a budget in GCP Console → Billing → Budgets & alerts |
| `Mail.Send permission denied` | Ensure Graph API app has `Mail.Send` as **application** (not delegated) permission and admin consent granted |

---

## 📄 License

MIT License — free to use and modify.
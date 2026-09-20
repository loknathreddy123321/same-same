# CFN Drift Fixer

Agentic CloudFormation drift detection and remediation system.

## Quick Start

### 1. Create virtual environment
```bash
python -m venv venv
venv\Scripts\activate.bat        # Windows CMD
# OR
source venv/bin/activate         # Mac/Linux
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Set up .env
```bash
copy .env.example .env           # Windows
# OR
cp .env.example .env             # Mac/Linux
```
Edit `.env` and add your `GROQ_API_KEY`

### 4. Get free Groq API key
Go to https://console.groq.com → Create API key → paste in .env

### 5. Run the agent (CLI)
```bash
set PYTHONPATH=src               # Windows CMD
python scripts/run_local.py --stack YOUR_STACK_NAME --region ap-south-1 --dry-run
```

### 6. Run the dashboard
```bash
set PYTHONPATH=src
python -m uvicorn dashboard.api:app --reload --port 8000
```
Open http://localhost:8000

## Running Locally (PowerShell, Windows)

Once the venv is created and `requirements.txt` / `dashboard` npm packages are installed, and `.env` has your `GROQ_API_KEY`, use these commands from the project root. Each runs in its own terminal.

### 1. Dashboard backend (FastAPI, port 8000)
```powershell
$env:PYTHONPATH = "."
.\venv\Scripts\python.exe -m uvicorn dashboard.api:app --port 8000
```
Open http://localhost:8000 for the static dashboard.

### 2. React dashboard (Vite, port 3000)
```powershell
cd dashboard
npm run dev
```
Open http://localhost:3000.

### 3. CLI only, no dashboard
```powershell
$env:PYTHONPATH = "src"
.\venv\Scripts\python.exe scripts\run_local.py --stack YOUR_STACK_NAME --region ap-south-1 --dry-run
```
Drop `--dry-run` to actually apply a fix (it prompts for interactive confirmation).

> If `venv\Scripts\Activate.ps1` fails or does nothing (PowerShell execution policy), skip activation entirely and call `.\venv\Scripts\python.exe` directly, as shown above — no need to activate.

## Project Structure
```
cfn-drift-fixer/
├── src/
│   ├── agent/
│   │   ├── graph.py        # LangGraph 8-node state machine
│   │   ├── nodes.py        # All 8 node functions
│   │   ├── cfn.py          # AWS CloudFormation operations
│   │   ├── state.py        # Shared state TypedDict
│   │   ├── bedrock.py      # LLM client (Groq / Bedrock)
│   │   ├── storage.py      # DynamoDB + S3
│   │   ├── safety.py       # 10 safety measures
│   │   └── prompts.py      # LLM prompts
│   └── notifications/
│       └── slack.py        # Slack notifications
├── dashboard/
│   ├── api.py              # FastAPI backend
│   └── index.html          # UI dashboard
├── scripts/
│   └── run_local.py        # CLI runner
├── requirements.txt
└── .env.example
```

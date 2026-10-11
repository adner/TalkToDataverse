# SpeedOfThought

Speak to Dataverse at the speed of thought. Hold to talk and say several requests in a row; each
one takes off as an agent run while you are still speaking.

- **L0** transcribes the microphone locally with NVIDIA Nemotron streaming ASR and detects pauses.
- **L1** asks Microsoft-Decision-1 about every word prefix: which kind of request (list, count,
  update, chart, open a record, follow-up…), which tables, and whether it refers to the previous
  request or answers a waiting question. A pause cuts the speech into segments.
- **L2** runs one `gpt-6-luna` flight per segment with typed record tools over the Dataverse Web
  API: query, count, get, create, update, delete, export, chart and open. Flights run in parallel,
  read each other's results, and can ask the operator a question.
- **L3** is a Next.js/CopilotKit page: the live probability race and transcript, and a card per
  flight with its tool calls, answer and chart.

The backend (L0–L2) is a FastAPI process on port 8000; the page (L3) is a Next.js dev server on
port 3000.
How the layers work, and the tools, events and settings in detail, is in
[`Architecture/architecture.md`](Architecture/architecture.md).

## Prerequisites

| What | Notes |
| --- | --- |
| Windows 10 or 11 | Microphone capture uses WASAPI. |
| NVIDIA GPU, recent driver | The ASR model runs on CUDA 12.8 (`cuda:0`). |
| A microphone | Chosen on the `/admin` page. |
| Python 3.12 and [uv](https://docs.astral.sh/uv/) | uv installs everything else on the Python side. |
| Node.js 22 and npm | For the page. |
| [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) | Dataverse tokens come from your CLI sign-in. |
| Azure AI Foundry | Deployments of Microsoft-Decision-1 and `gpt-6-luna` (step 2). |
| A Dataverse environment | With the Contoso Bikes tables (step 3). |

## Setup

### 1. Get the code and install

```powershell
git clone <this repository>
Set-Location SpeedOfThought
uv sync --extra live
Set-Location apps/presentation
npm ci
Set-Location ../..
```

Use `--extra live`: a plain `uv sync` leaves out the microphone, ASR and L2 packages. Run every
`uv run` command below from the repository root.

### 2. Foundry deployments and keys

In Azure AI Foundry, create:

- a **Microsoft-Decision-1** deployment named `Decision-1`. Each deployment has its own
  requests-per-minute quota, and L1 calls it on every word, so you can list several deployments
  as pools;
- a **`gpt-6-luna`** deployment, called through the OpenAI-compatible Responses API.

Save each key, alone on one line, in a text file in the repository root, e.g. `foundrykey.txt`
and `foundrykey2.txt`. Files named `foundrykey*.txt` are git-ignored; `config/sot.toml` names the
file each endpoint reads.

### 3. Dataverse environment

The agent works against a fixed data model: `account` plus the custom `cb_` tables (bike models,
sales agreements and orders, registered bikes, warranty cases, returns, suppliers, parts,
purchase orders, quality incidents…). [`data/contoso/schema.json`](data/contoso/schema.json) lists
every table and column it expects. The solution that creates these tables and their demo data is
not part of this repository.

You also need:

- a user with read and write access to those tables;
- a model-driven app with forms for the tables you want to open by voice ("open Nordic Rental's
  critical order"). Note its app ID, the `appid` in the app's URL.

Sign the Azure CLI in to the environment's tenant once:

```powershell
az login --tenant <your-entra-tenant-id> --allow-no-subscriptions
```

### 4. Configure

Fill in the placeholders in [`config/sot.toml`](config/sot.toml):

| Setting | Value |
| --- | --- |
| `[[l1.decision_pools]]` `endpoint`, `key_file` | `https://<resource>.services.ai.azure.com/providers/microsoft/v1/systemone` and its key file. One entry per Decision-1 deployment; delete the second entry if you have one. `deployment` defaults to `Decision-1`. |
| `[l2]` `endpoint`, `key_file` | `https://<resource>.services.ai.azure.com/openai/v1/` and its key file. |
| `[l2]` `dataverse_url` | `https://<org>.crm.dynamics.com` |
| `[l2]` `tenant_id` | The environment's Entra tenant ID. |
| `[l2.record_app]` | The app's `name`, `app_id` and the `tables` it has forms for. Requests to open other tables are refused. |

[`config/l0.toml`](config/l0.toml) holds the ASR model and audio settings; the defaults work.

### 5. Snapshot the demo data

Spoken writes really change the environment. Record its current state as the baseline you can go
back to:

```powershell
uv run sot-reset snapshot
```

This writes `data/contoso/schema.json` and `data/contoso/baseline.json` from your environment.
Later, `uv run sot-reset restore --dry-run` shows what changed and `uv run sot-reset restore`
puts it back. Restore deletes every record created after the snapshot, so only point it at a
demo environment.

## Run

Start the backend:

```powershell
uv run sot-backend
```

The first start downloads the Nemotron ASR model from Hugging Face. In a second terminal, start
the page:

```powershell
Set-Location apps/presentation
npm run dev
```

Open <http://localhost:3000>.

1. Go to <http://localhost:3000/admin> and check that it says **Ring buffer armed** with the right
   input device. To change the device, pick it and press **Save and re-arm**.
2. Back on the stage, hold **Hold to talk**, speak, and release. Pause about half a second between
   requests, and a full second around writes ("escalate them", "delete…"). For example: "How many
   bike models do we have … show the open warranty cases per customer as a bar chart".
3. Reload the page to start a new session; thread numbers start again at T01.

Stop each process with `Ctrl+C`.

### Opening records

"Open that order" shows the record in a dialog that frames your model-driven app. A frame cannot
sign in, so open the app once in a normal tab of the same browser first (Edge or Chrome, not a
private window). If the environment enforces a content security policy, allow
`http://localhost:3000` to frame it. Close the dialog with **Esc**.

### Without a microphone

On `/admin`, tick **Typed input on the stage** to get a text box next to Hold to talk. It is saved
in that browser only.

### From another device

To open the page from another machine on your network, add its address to `[l3] origins` in
`config/sot.toml` (e.g. `http://192.168.1.20:3000`) and set `SOT_DEV_ORIGINS=192.168.1.20` in
`apps/presentation/.env.local`. The backend URL the page uses is `NEXT_PUBLIC_SOT_BACKEND_URL`
(default `http://127.0.0.1:8000`).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Dataverse calls fail with 401 | Run the `az login --tenant …` above; check `tenant_id`. |
| L1 or L2 calls fail with 401 or 404 | Check the endpoints, deployment names and key files. |
| L1 lags or reports 429 | The Decision-1 quota is exhausted: add a pool, or lower `[l1] decision_rpm`. |
| The record dialog says it refused to connect | Sign in to the app in a normal tab (see Opening records). |
| `/admin` shows no armed buffer | Pick the microphone and press Save and re-arm; check the backend log. |
| A write merged with the request before it | Pause a full second before and after writes. |

Diagnostics, including every Decision-1 call and flight, are on the **Flight recorder** link on
`/admin`, and in `logs/sot-diagnostics.jsonl` (git-ignored).

## License

[MIT](LICENSE) © 2026 Andreas Adner.

# Automation Studio (Home Assistant AI Automation Builder)

Automation Studio is a local-first UI and API that helps you build, edit, and manage Home Assistant automations and scripts with AI assistance. It includes a knowledgebase (`capabilities.yaml`) so the system can learn your conventions and devices over time, plus a Home Assistant add-on wrapper for easy deployment.

## What This Repo Includes
- FastAPI backend (`agent_server.py`)
- Web UI (`static/`)
- Home Assistant add-on wrapper (`automation_studio/`)
- AI prompt files for all agents
- Example knowledgebase (`capabilities.example.yaml`)

## Quick Start (Local)
1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set environment variables (example):
   ```bash
   set HA_URL=http://homeassistant.local:8123
   set HA_TOKEN=YOUR_LONG_LIVED_TOKEN
   set AUTOMATIONS_FILE_PATH=/config/automations.yaml
   set SCRIPTS_FILE_PATH=/config/scripts.yaml
   set RESTORE_STATE_PATH=/config/.storage/core.restore_state
   set CAPABILITIES_FILE=capabilities.yaml
   ```
3. Start the server:
   ```bash
   uvicorn agent_server:app --host 0.0.0.0 --port 8124
   ```
4. Open the UI in your browser:
   ```text
   http://localhost:8124
   ```

## Home Assistant Add-on
The add-on lives in `automation_studio/`.

### Install (GitHub repo)
1. Go to **Settings -> Add-ons -> Add-on Store**.
2. Open the menu (three dots) -> **Repositories**.
3. Add your GitHub repository URL.
4. Install **Automation Studio** and start it.

### Install (Local repo path)
If you run HA OS and can mount a local repo, add its path in the same **Repositories** screen.

### Add-on configuration
All secrets and paths are configured in the add-on UI. No secrets are stored in the repo.

## Knowledgebase (capabilities.yaml)
Create your own knowledgebase file by copying the example:
```bash
copy capabilities.example.yaml capabilities.yaml
```

The file is not committed to git (see `.gitignore`). It stores your entities, conventions, and learned notes.

Key sections:
- `notifications.primary_phone_notify` for phone notifications
- `media.power_toggle_rules` for TV power scripts
- `covers.position_rules` for custom open/close behavior

## AI Agent Setup (Required for best results)
Create conversation agents in Home Assistant and paste prompts from the repo.

Suggested agents (models are suggestions; use what you prefer):
- Architect: `architect_prompt.txt` (gpt-5.2)
- Builder (main): `builder_prompt.txt` (gpt-5.2)
- Dumb builder fallback: `builder_prompt.txt` + `dumb_builder_addendum.txt` (gpt-4o-mini)
- Summary: `automation_summary_prompt.txt` (gpt-4o-mini)
- Capability mapper: `capability_mapper_prompt.txt` (gpt-4o-mini)
- Semantic diff summarizer: `semantic_diff_prompt.txt` (gpt-4o-mini)
- Knowledgebase helper: `kb_sync_helper_prompt.txt` (gpt-4o-mini)

### Configure agent IDs
You can override agent IDs in three places (highest priority first):
1. UI Settings -> AI agents (runtime config).
2. Add-on configuration (add-on) or environment variables (local).
3. Defaults in `agent_server.py`.

In the UI, open **Settings** and select each agent from the dropdowns. The list is pulled from Home Assistant conversation agents, and leaving a dropdown blank uses the server default.

Environment variable names (local):
- `BUILDER_AGENT_ID`
- `ARCHITECT_AGENT_ID`
- `SUMMARY_AGENT_ID`
- `CAPABILITY_MAPPER_AGENT_ID`
- `SEMANTIC_DIFF_AGENT_ID`
- `KB_SYNC_HELPER_AGENT_ID`
- `DUMB_BUILDER_AGENT_ID`

## Repository Metadata
If you plan to publish the add-on, update `repository.yaml` (and `repository.json`) with your name and repo URL.

## Security
- Keep tokens and secrets out of the repo.
- Use the add-on config or environment variables for secrets.
- `.gitignore` excludes local state, DB files, and `capabilities.yaml`.

## Support / Troubleshooting
Common issues:
- **401 Unauthorized**: set `AGENT_SECRET` consistently in UI and server.
- **No automations**: check `AUTOMATIONS_FILE_PATH`.
- **KB view empty**: ensure `capabilities.yaml` exists and is readable.

## License
MIT License

Copyright (c) 2026 Jack Hopperton

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

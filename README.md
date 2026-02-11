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

Main agents:
- Architect: `architect_prompt.txt`
- Builder: `builder_prompt.txt`
- Dumb builder fallback: `builder_prompt.txt` + `dumb_builder_addendum.txt`

Helper agents:
- Summary: `automation_summary_prompt.txt`
- Capability mapper: `capability_mapper_prompt.txt`
- Semantic diff summarizer: `semantic_diff_prompt.txt`
- Knowledgebase helper: `kb_sync_helper_prompt.txt`

Use your preferred models in each agent. The default IDs are set in `agent_server.py` and can be overridden with env vars.

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
Add a license of your choice before publishing.

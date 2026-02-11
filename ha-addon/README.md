# Automation Studio Home Assistant Add-on

This folder contains a Home Assistant add-on wrapper for the Automation Studio UI/API.

## Install (local add-on repository)
1. In Home Assistant, go to **Settings -> Add-ons**.
2. Click **Add-on Store**.
3. In the top-right menu (...), choose **Repositories**.
4. Add the path to your local repo (e.g. `C:\path\to\ha-auto-agent`).
   - If running HA OS, you can instead add a Git repository URL.
5. You should see **Automation Studio** in the list. Click it and **Install**.

## First-time config
- Open the add-on configuration and set:
- `ha_token` (optional; leave blank to use the Supervisor token)
- `agent_secret` (optional shared secret for API requests)
- `automations_path` (defaults to `/config/automations.yaml`)
- `scripts_path` (defaults to `/config/scripts.yaml`)
- `restore_state_path` (defaults to `/config/.storage/core.restore_state`)

No personal data or API keys are stored in the repo files. Set secrets only in the add-on configuration.

## Capabilities file
`capabilities.yaml` is the user-specific knowledge base and normalization map.
Start from `capabilities.example.yaml` and fill in your own entity IDs.

Key sections used by the engine:
- `media.power_toggle_rules` (rewrite power on/off to a script toggle).
- `covers.position_rules` (rewrite open/close to set_cover_position).

Completion announcements are off by default. To enable them, set environment variables:
`CONFIRM_DOMAIN`, `CONFIRM_SERVICE`, and `CONFIRM_FIELD`.

## Start
- Start the add-on.
- Open it via the left sidebar (Ingress).

## AI agent setup (required for best results)
Create the following conversation agents in Home Assistant and paste the prompts from the repo.
Use the indicated model for each.

### Main agents
1) **Architect**
   - Agent ID: `conversation.automation_architect`
   - Model: `gpt-5.2`
   - Prompt file: `architect_prompt.txt`

2) **Builder (main)**
   - Agent ID: `conversation.homeassistant`
   - Model: `gpt-5.2`
   - Prompt file: `builder_prompt.txt`

3) **Dumb builder (fallback)**
   - Agent ID: `conversation.autoautomation_dumb_builder`
   - Model: `gpt-4o-mini`
   - Prompt file: `builder_prompt.txt` + append `dumb_builder_addendum.txt`

### Helper agents (cheap models)
4) **Summary**
   - Agent ID: `conversation.automation_summary`
   - Model: `gpt-4o-mini`
   - Prompt file: `automation_summary_prompt.txt`

5) **Capability mapper**
   - Agent ID: `conversation.home_assistant_capability_mapper`
   - Model: `gpt-4o-mini`
   - Prompt file: `capability_mapper_prompt.txt`

6) **Semantic diff summarizer**
   - Agent ID: `conversation.home_assistant_semantic_diff_summarizer`
   - Model: `gpt-4o-mini`
   - Prompt file: `semantic_diff_prompt.txt`

7) **Knowledgebase sync helper**
   - Agent ID: `conversation.knowledgebase_sync_helper`
   - Model: `gpt-4o-mini`
   - Prompt file: `kb_sync_helper_prompt.txt`

After creating the agents, restart the add-on so it can use them.

## Notes
- Data is stored in `/data` inside the add-on container (persistent).
- Use the main project repo for updates; rebuild add-on after changes.

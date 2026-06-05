# Paste into each agent's MCP config

## Codex  (~/.codex/config.toml)
```toml
[mcp_servers.harn]
command = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
args = ["-m", "harn", "mcp"]
```

## Antigravity  (~/.gemini/config/mcp_config.json)
```json
{
  "mcpServers": {
    "harn": {
      "command": "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
      "args": [
        "-m",
        "harn",
        "mcp"
      ],
      "env": {
        "HARN_ENV_DIR": "harn_env"
      }
    }
  }
}
```

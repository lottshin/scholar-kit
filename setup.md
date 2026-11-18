# Scholar Kit — Auto Install

You are an AI agent. Follow these steps to install or update Scholar Kit cleanly.

## Steps

1. Detect the current agent platform and choose `<target_path>`:

   | Platform | Target Skill Path |
   |----------|-------------------|
   | **Claude Code** | `~/.claude/skills/scholar-kit` |
   | **Codex** | `~/.codex/skills/scholar-kit` |
   | **Antigravity** (Global / Workspace) | `~/.gemini/config/skills/scholar-kit` or `<workspace>/.agents/skills/scholar-kit` |
   | **Cursor** (macOS / Linux) | `~/.cursor/skills/scholar-kit` |
   | **Cursor** (Windows) | `$env:USERPROFILE\.cursor\skills\scholar-kit` |
   | **Gemini CLI** | `~/.gemini/skills/scholar-kit` |
   | **Multi-Agent Machine** | Clone once to `~/.agents/skills/scholar-kit` and symlink other agent skill paths to it |

2. If `<target_path>` already exists:
   - If it is a git repository (`git -C <target_path> rev-parse --is-inside-work-tree`), run `git -C <target_path> pull`.
   - Otherwise, inform the user it is a non-git installation and stop unless they want to reinstall.

3. If `<target_path>` does not exist, clone the repository:

   ```bash
   git clone https://github.com/lottshin/scholar-kit <target_path>
   ```

4. Install Python dependencies (compatible with PEP 668 externally-managed Python environments):

   ```bash
   python3 -m pip install -r <target_path>/scripts/requirements.txt || (
     python3 -m venv ~/.scholar-kit/.venv &&
     ~/.scholar-kit/.venv/bin/python -m pip install -r <target_path>/scripts/requirements.txt
   )
   ```
   *(On Windows, use `py -3` and `$env:USERPROFILE\.scholar-kit\.venv\Scripts\python.exe` if needed.)*

5. Verify the environment:

   ```bash
   python3 <target_path>/scripts/literature.py check --fix
   ```

6. Tell the user:
   "Scholar Kit 安装/更新完成！直接对我说「帮我搜关于 XX 的论文」即可开始使用。"

# Scholar Kit — Auto Install

You are an AI agent. Follow these steps to install Scholar Kit.

## Steps

1. Detect the platform and set the target skill directory:

   | Platform | Target Path |
   |----------|-------------|
   | **Cursor** (Windows) | `$env:USERPROFILE\.cursor\skills\scholar-kit` |
   | **Cursor** (macOS/Linux) | `~/.cursor/skills/scholar-kit` |
   | **Claude Code / Codex** | `~/.codex/skills/scholar-kit` |
   | **Gemini CLI** | `~/.gemini/skills/scholar-kit` |
   | **Other** | Ask the user where to install |

2. If the target directory already exists:
   - Check if it's a git repo (`git -C <target_path> rev-parse --is-inside-work-tree`)
   - If yes: run `git -C <target_path> pull` and `pip install -r <target_path>/scripts/requirements.txt`, then tell the user "Scholar Kit 已更新！"
   - If not a git repo: tell the user "Scholar Kit 已安装（非 git 安装，如需更新请重新下载）" and stop.

3. Clone the repository (Gemini CLI can also use `gemini skills install`):

   ```bash
   git clone https://github.com/lottshin/scholar-kit <target_path>
   ```

4. Install Python dependencies:

   ```bash
   pip install -r <target_path>/scripts/requirements.txt
   ```

5. Verify by running:

   ```bash
   python <target_path>/scripts/literature.py check
   ```

6. Tell the user:
   "Scholar Kit 安装完成！开启新会话后，直接说'帮我搜论文'即可使用。"

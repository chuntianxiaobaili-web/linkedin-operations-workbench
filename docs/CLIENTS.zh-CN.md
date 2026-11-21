# AI Agent 安装与使用

## 打开完整项目

下载仓库的完整 ZIP 并解压，或运行以下命令。用客户端打开包含 `SKILL.md` 的目录。

```sh
git clone https://github.com/chuntianxiaobaili-web/linkedin-operations-workbench.git
cd linkedin-operations-workbench
```

保留 `SKILL.md`、`scripts/`、`references/`、`examples/` 和 `vault/` 的相对位置。支持访问项目文件和运行命令的客户端可以使用全部本地功能。只有文件上传能力的客户端可以读取 `SKILL.md` 和任务相关资料，用于方法归纳、研究整理或草稿撰写。需要本地程序的操作应在有终端的环境运行。

先让客户端运行本地检查。若 `python` 命令不可用，可使用 `python3` 或 Windows 的 `py -3`，并核对 Python 版本至少为 3.10。

```sh
python -B -X utf8 scripts/workbench.py doctor
python -B -X utf8 scripts/workbench.py vault verify
```

Obsidian 用于浏览和编辑知识库。安装后可打开仓库内的 `vault/`。Markdown 和本地命令也可以独立使用。

## 通过客户端的技能入口调用

按下表创建客户端对应的 `linkedin-operations-workbench` 文件夹，把 `SKILL.md` 和上文列出的四个配套目录复制到技能文件夹根层。打开项目后按表中方式调用。任一客户端都可直接让 Agent 读取项目根目录的 `SKILL.md`，并由此读取其余文件。

| 客户端 | 项目级技能目录 | 调用方式与官方文档 |
| --- | --- | --- |
| Codex | `.agents/skills/linkedin-operations-workbench/` | 在支持技能选择的界面输入 `$` 并选择 `linkedin-operations-workbench`。新技能未显示时重开项目或重启客户端。见[Codex 技能加载目录](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。 |
| Claude Code | `.claude/skills/linkedin-operations-workbench/` | 输入 `/linkedin-operations-workbench`。在云工作区中，将技能文件放入该工作区能读取的项目目录。见[Claude Code 项目技能](https://code.claude.com/docs/en/skills#choose-where-skills-load)。 |
| DeepSeek Harness | `.dsh/skills/linkedin-operations-workbench/` | 在 Harness 中打开项目，要求使用 `linkedin-operations-workbench` 技能。官方 Harness 也支持 `.agents/skills/` 作为项目技能目录。见[DeepSeek Harness](https://www.deepseek.com/harness/en/)和[本地技能发现规则](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.zh.md)。 |
| Grok Build | `.grok/skills/linkedin-operations-workbench/` | 输入 `/linkedin-operations-workbench`。这里的路径对应有本地项目功能的 Grok Build。见[Grok Build 技能](https://docs.x.ai/build/features/skills-plugins-marketplaces#skills)。 |

**WorkBuddy**。在能访问本地项目文件的任务中，直接打开完整项目并指定根目录的 `SKILL.md`。若要通过客户端原生技能入口导入，另建 `linkedin-operations-workbench/` 文件夹，复制 `SKILL.md` 和配套目录，按腾讯文档为导入副本增加 WorkBuddy 所需的展示名称、描述、版本和作者字段。压缩包顶层保留该技能目录，进入「专家·技能·连接器 → 技能 → 添加技能 → 上传技能」选择压缩包。见[WorkBuddy 技能市场与导入](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)和[技能打包规范](https://open.workbuddy.cn/docs/skill)。

**Your Dot**。桌面连接已启用且具备项目访问权限时，可由 Your Dot 启动本地任务，指定项目文件夹及根目录 `SKILL.md`。文件访问与命令运行使用该本地任务实际可用的能力。见[Your Dot 入门说明](https://help.openai.com/en/articles/20001530-getting-started-with-your-dot)。

云电脑和远程工作区使用各自能访问的仓库副本，并在该环境运行自检。客户端入口说明依据官方文档整理。单一环境的自检仅覆盖该环境中的程序和知识库，不能据此判断其他客户端的登录、发送或发布结果。

## 第一次运行的提示词

> 读取项目根目录的 `SKILL.md`。行业是室内照明，目标客户是加拿大的酒店采购商。从打包知识库选出相关方法。根据我随后提供的公司资料，逐家记录公开业务事实、来源、匹配理由和待核验信息，并给每家写一条英文开场和中文审阅稿。最后给出可复用的审核清单。本次仅生成研究记录和草稿。

要建立新行业的知识库，把最后两句改为「根据你的已有知识输出方法卡，说明适用条件、判断依据和常见误区。用本地 `knowledge` 命令创建独立 Markdown 知识库」。

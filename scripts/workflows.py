"""
workflows.py - 预定义工作流模板

Agent 可以直接调用这些模板，无需每次重新组合命令。
每个工作流包含：
- name: 工作流名称
- description: 工作流描述
- steps: 命令步骤列表
- variables: 需要替换的变量
- expected_output: 预期输出
"""

import shlex
from typing import Dict, List, Any

WORKFLOW_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "literature_review_classic": {
        "name": "经典文献综述",
        "description": "搜索高被引经典论文 + 最新研究进展 + 主题聚类分析",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "openalex",
                    "sort": "citations",
                    "year_from": 2015,
                    "limit": 20
                },
                "description": "搜索高被引经典论文（2015年至今）"
            },
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "openalex",
                    "sort": "date",
                    "year_from": 2023,
                    "limit": 10,
                    "append": True
                },
                "description": "补充最新研究进展（2023年至今）"
            },
            {
                "command": "review",
                "args": {
                    "cluster": True,
                    "gaps": True
                },
                "description": "生成主题聚类和研究空白分析"
            }
        ],
        "variables": ["topic"],
        "expected_output": "综述材料 + 主题聚类 + 研究空白提示",
        "estimated_time_seconds": 15
    },

    "high_quality_screening": {
        "name": "高质量论文筛选",
        "description": "按质量评分筛选高质量论文，可选期刊过滤",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "openalex",
                    "sort": "quality",
                    "year_from": "{year_from}",
                    "journal_filter": "{journal}",
                    "limit": 30
                },
                "description": "按质量评分排序，可选期刊过滤"
            }
        ],
        "variables": ["topic", "year_from", "journal"],
        "expected_output": "按质量评分排序的论文列表（质量分数 0-100）",
        "estimated_time_seconds": 5
    },

    "citation_suggestion": {
        "name": "引用建议",
        "description": "读取论文 + 搜索匹配文献 + 获取详情",
        "steps": [
            {
                "command": "read-paper",
                "args": {
                    "filepath": "{file}"
                },
                "description": "读取用户论文，提取关键信息"
            },
            {
                "command": "search",
                "args": {
                    "query": "{keywords}",
                    "source": "openalex",
                    "sort": "quality",
                    "limit": 15
                },
                "description": "搜索匹配的高质量文献"
            },
            {
                "command": "read-detail",
                "args": {
                    "indices": "{selected}"
                },
                "description": "获取选中论文的详细信息"
            }
        ],
        "variables": ["file", "keywords", "selected"],
        "expected_output": "推荐引用的论文列表 + 详细摘要",
        "estimated_time_seconds": 20
    },

    "topic_research": {
        "name": "选题分析",
        "description": "多源搜索 + 选题建议 + 证据校验",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "all",
                    "limit": 30,
                    "project": "{project}"
                },
                "description": "多源搜索，建立课题文献库"
            },
            {
                "command": "topics",
                "args": {
                    "project": "{project}"
                },
                "description": "基于文献库生成选题建议"
            },
            {
                "command": "validate",
                "args": {
                    "project": "{project}"
                },
                "description": "校验选题的证据支撑质量"
            }
        ],
        "variables": ["topic", "project"],
        "expected_output": "选题建议 + 证据编号 + 支撑强度评估",
        "estimated_time_seconds": 25
    },

    "author_tracking": {
        "name": "作者追踪",
        "description": "追踪特定学者的最新研究",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "openalex",
                    "author_filter": "{author}",
                    "sort": "date",
                    "limit": 20
                },
                "description": "搜索特定作者的最新论文"
            }
        ],
        "variables": ["topic", "author"],
        "expected_output": "作者最新论文列表（按时间排序）",
        "estimated_time_seconds": 5
    },

    "journal_screening": {
        "name": "期刊定向检索",
        "description": "在特定期刊中搜索相关论文",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "openalex",
                    "journal_filter": "{journal}",
                    "sort": "citations",
                    "year_from": "{year_from}",
                    "limit": 20
                },
                "description": "在指定期刊中搜索高被引论文"
            }
        ],
        "variables": ["topic", "journal", "year_from"],
        "expected_output": "特定期刊的相关论文列表",
        "estimated_time_seconds": 5
    },

    "interdisciplinary_research": {
        "name": "跨学科研究",
        "description": "多学科领域文献搜索 + 去重 + 质量筛选",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "all",
                    "sort": "quality",
                    "limit": 50
                },
                "description": "多源搜索，自动去重"
            }
        ],
        "variables": ["topic"],
        "expected_output": "跨数据源去重后的高质量论文列表",
        "estimated_time_seconds": 10
    },

    "citation_network_analysis": {
        "name": "引文网络分析",
        "description": "分析论文的引用关系（前向+后向）",
        "steps": [
            {
                "command": "citations",
                "args": {
                    "identifier": "{doi_or_url}",
                    "direction": "both",
                    "limit": 20
                },
                "description": "获取双向引文网络"
            }
        ],
        "variables": ["doi_or_url"],
        "expected_output": "引用该论文的文献 + 该论文引用的文献",
        "estimated_time_seconds": 8
    },

    "trend_analysis": {
        "name": "研究趋势分析",
        "description": "搜索 + 趋势统计（年份分布、热点关键词）",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "openalex",
                    "limit": 100
                },
                "description": "搜索大量相关文献"
            },
            {
                "command": "trends",
                "args": {},
                "description": "分析年份分布、高频关键词、高被引论文"
            }
        ],
        "variables": ["topic"],
        "expected_output": "年份分布 + Top 30 关键词 + Top 10 高被引论文",
        "estimated_time_seconds": 12
    },

    "thesis_collection": {
        "name": "学位论文收集",
        "description": "搜索硕博士论文（仅知网）",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "cnki",
                    "doc_type": "thesis",
                    "year_from": "{year_from}",
                    "limit": 20
                },
                "description": "搜索学位论文（硕士+博士）"
            }
        ],
        "variables": ["topic", "year_from"],
        "expected_output": "学位论文列表",
        "estimated_time_seconds": 10,
        "requirements": ["cnki_feasible"]
    },

    "core_journal_screening": {
        "name": "核心期刊筛选",
        "description": "搜索核心期刊论文（仅知网）",
        "steps": [
            {
                "command": "search",
                "args": {
                    "query": "{topic}",
                    "source": "cnki",
                    "core": "{core_type}",
                    "year_from": "{year_from}",
                    "limit": 20
                },
                "description": "搜索核心期刊论文"
            }
        ],
        "variables": ["topic", "core_type", "year_from"],
        "expected_output": "核心期刊论文列表",
        "estimated_time_seconds": 10,
        "requirements": ["cnki_feasible"],
        "notes": "core_type 可选: 北大核心, CSSCI, CSCD, EI 等"
    }
}


def get_workflow(workflow_id: str) -> Dict[str, Any]:
    """获取指定工作流模板"""
    return WORKFLOW_TEMPLATES.get(workflow_id)


def list_workflows(category: str = None) -> List[Dict[str, Any]]:
    """列出所有工作流模板"""
    workflows = []
    for wf_id, wf in WORKFLOW_TEMPLATES.items():
        workflows.append({
            "id": wf_id,
            "name": wf["name"],
            "description": wf["description"],
            "variables": wf["variables"],
            "estimated_time_seconds": wf.get("estimated_time_seconds", 10),
            "requirements": wf.get("requirements", [])
        })
    return workflows


POSITIONAL_ARGS = {
    "search": {"query"},
    "read-paper": {"filepath"},
    "citations": {"identifier", "paper_id"},
    "download": {"target"},
    "detail": {"url"},
    "import": {"filepath"},
    "pdf-meta": {"filepath"},
    "write-docx": {"filepath"},
}


def _render_arg_value(arg_value: Any, variables: Dict[str, str]) -> Any:
    if isinstance(arg_value, str) and "{" in arg_value:
        for var_name, var_value in variables.items():
            arg_value = arg_value.replace(f"{{{var_name}}}", str(var_value))
    return arg_value


def render_workflow_argv(workflow_id: str, variables: Dict[str, str]) -> List[List[str]]:
    """渲染工作流为 argv 列表，供 subprocess 直接执行。"""
    workflow = get_workflow(workflow_id)
    if not workflow:
        return []

    commands: List[List[str]] = []
    for step in workflow["steps"]:
        command = step["command"]
        cmd_parts = [command]
        positional_args = POSITIONAL_ARGS.get(command, set())

        for arg_name, arg_value in step["args"].items():
            arg_value = _render_arg_value(arg_value, variables)

            # 构建命令参数
            if isinstance(arg_value, bool):
                if arg_value:
                    cmd_parts.append(f"--{arg_name.replace('_', '-')}")
            elif arg_name in positional_args:
                cmd_parts.append(str(arg_value))
            else:
                cmd_parts.extend([f"--{arg_name.replace('_', '-')}", str(arg_value)])

        commands.append(cmd_parts)

    return commands


def render_workflow(workflow_id: str, variables: Dict[str, str]) -> List[str]:
    """
    渲染工作流为可展示的命令列表。

    Args:
        workflow_id: 工作流 ID
        variables: 变量替换字典，如 {"topic": "deep learning", "year_from": "2020"}

    Returns:
        命令字符串列表
    """
    return [" ".join(shlex.quote(part) for part in argv)
            for argv in render_workflow_argv(workflow_id, variables)]


def validate_workflow_requirements(workflow_id: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
    """
    验证工作流的前置条件是否满足

    Args:
        workflow_id: 工作流 ID
        capabilities: 从 check 命令获取的能力字典

    Returns:
        {"satisfied": bool, "missing": List[str], "suggestions": List[str]}
    """
    workflow = get_workflow(workflow_id)
    if not workflow:
        return {"satisfied": False, "missing": ["workflow_not_found"], "suggestions": []}

    requirements = workflow.get("requirements", [])
    missing = []
    suggestions = []

    for req in requirements:
        if req == "cnki_feasible" and not capabilities.get("cnki_feasible"):
            missing.append("cnki_feasible")
            suggestions.append("需要知网访问权限，请确认已连接校园网/VPN")
        elif req == "docx_tools" and not capabilities.get("docx_tools"):
            missing.append("docx_tools")
            suggestions.append("需要 python-docx 库，运行: pip install python-docx")

    return {
        "satisfied": len(missing) == 0,
        "missing": missing,
        "suggestions": suggestions
    }

<!-- → authority: 00-meta.md -->
# Model Router Rule

## Purpose
Optimize token usage and success rates by matching the complexity of a task to the capability of the currently active LLM.

## Task Complexity Classification

| Level | Criteria | Recommended Model |
|-------|---------|-------------------|
| 🟢 Simple | Single file, < 50 lines changed, follows existing pattern, no new dependencies | Local Small |
| 🟡 Medium | 2-5 files, understanding dependencies, testing needed | Local Medium |
| 🔴 Complex | Architectural decisions, 5+ files, new patterns | Cloud Large |

## Auto-Classification Heuristics

The current task is **🟢 Simple** if **ALL** of the following are true:
- Affects only one file
- Changes fewer than 50 lines of code
- Follows an existing pattern in the codebase (use `mem_search` with tag "pattern")
- No new external dependencies (packages, libraries)
- No database schema changes

The current task is **🔴 Complex** if **ANY** of the following are true:
- Affects 5 or more files
- Introduces a new architectural pattern or design decision
- Requires creating new database tables or significant schema changes
- Involves integrating a new external service or API
- Requires writing more than 200 lines of new code

All other tasks are **🟡 Medium**.

## Escalation Protocol

If you encounter repeated failures, output format errors, circular dependencies, or context issues:
1. STOP and warn the user: *"⚠️ Model router escalation: task complexity may exceed current model capabilities."*
2. Recommend switching to a more capable model or breaking down the task.
3. If the user insists on continuing, proceed but log warnings via memory (use `mem_tag_entity` as defined in `01-memory-bank.md`).

## Universal Model Awareness

- **High-Speed Cloud Models (e.g. Gemini 3 Pro/Flash, GPT-4o-mini, Claude 3.5 Haiku, DeepSeek v4 Pro/Flash, etc.)**: If active, you are fully authorized to handle 🔴 Complex tasks and large context scopes eagerly, but you MUST strictly maintain Native Tool Priority to guarantee execution speed.

### API Response Strictness (Anti-Parse Error Protocol)

To prevent `Invalid API Response` (empty or unparsable response) errors:
1. **Valid Tool Formats**: Always output tool calls strictly matching the provided schema. Do not hallucinate non-existent tools or parameters.
2. **No Markdown Wrapping**: Do not wrap JSON or XML tool calls inside markdown code blocks (````json ... ````) unless explicitly requested by the environment. Output raw tool schemas.
3. **Never Empty**: Never return a completely empty response. Always include a brief thought process before executing a tool.
4. **Valid JSON**: Ensure string escapes and JSON structures are flawlessly formatted.

### Anti‑Malformed Tool Call Rules (MANDATORY)

To prevent `Invalid API Response` or `Connection closed` errors, you MUST follow these rules for **every** tool call:

1. **Never wrap tool arguments in markdown code blocks**  
   - ❌ WRONG: `` `json { "key": "value" } ` ``  
   - ✅ CORRECT: Raw JSON string inside `<arguments>` tag.

2. **Always include both opening and closing tags**  
   - For any tool call, the XML structure must be complete:  
     `<tool_name>`  
     `<param1>value1</param1>`  
     ...  
     `</tool_name>`  
   - For `use_mcp_tool`, the `<arguments>` tag MUST have a matching `</arguments>`.

3. **Use exact parameter names** as defined in the tool schema.  
   - Example: `mem_create_entities` expects parameter `entities` (not `entitiesList` or `entity`). Dict keys inside must also match: e.g., `entityType` not `entity_type`.

4. **Do not add extra text before or after the tool call** – the entire response should contain only the valid XML.

5. **If a tool call fails due to malformed XML, do not retry the same format**. Ask the user for help or use a different approach.

6. **When using `task_progress`, place it as the last inner element** – never before or after the closing tag of the tool call.  
   Example:
   ```xml
   <use_mcp_tool>
   <server_name>awlab-memory</server_name>
   <tool_name>mem_create_entities</tool_name>
   <arguments>
   {"entities": [{"name": "Test", "type": "example"}]}
   </arguments>
   <task_progress>- [x] Step done</task_progress>
   </use_mcp_tool>
   ```

### Native Tool Priority (REPL Hallucination Prevention)

You MUST prioritize native system tools (`view_file`, `replace_file_content`, `grep_search`, `write_to_file`) over raw terminal commands (`cat`, `sed`, `grep`, `echo`, `powershell`).

Raw shell execution is restricted to tasks where no native tool exists (e.g., running build scripts, tests).

**EXCEPTION - Permitted Shell Commands:**

The following operations are explicitly permitted and do NOT violate the Native Tool Priority rule:
- Any command explicitly whitelisted by other rule files with the `[PERMITTED]` marker
- Native shell test runs (e.g., `npm test`, `pytest`) and project build commands (e.g., `npm run dev`, `npm run build`)
- Git commands used during active version auditing or scans (e.g., `git status --porcelain`)
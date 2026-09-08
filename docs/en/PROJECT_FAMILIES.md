# 👨‍👩‍👧‍👦 Project Families Configuration

> [🏠 README](../../README.md) · [📚 Docs](../../README.md#documentation) · **Project Families**

> **TL;DR:** To share memory and code-graphs across related projects (e.g. backend + frontend), create a `~/.awlab-id/agent-memory/project-families.json` file on your machine grouping their paths. The AI will automatically detect it.

A **Project Family** allows you to logically group multiple correlated projects (such as a frontend repository and a separate backend repository). By configuring a project family, the MCP system will enable:

1. **Merged Code Knowledge Graphs:** When `graph_build` is called with the `family` parameter, the system builds a unified, cross-project codebase graph where nodes are explicitly tagged with their respective `project_id::`.
2. **Shared Episodic Memory:** AI agents will have access to a shared memory store for the entire family (`family_<slug>`), allowing them to reuse learned architectural patterns, business logic, or code conventions across all member projects.

## 👤 Who Handles the Configuration?

> [!IMPORTANT]
> **Project families must be configured manually by the developer (you).**

The MCP server and AI agents do not automatically guess or create these configurations because deciding which projects belong together is a high-level architectural decision that requires human insight.

> Once you manually set up the configuration file, the MCP system automatically takes over and handles all the backend orchestration (graph merging, memory sharing, ID reconciliation) completely autonomously.

## 🛠️ Best Practice Setup

The best practice for configuring a project family is to manually create a JSON declaration file in the global AWLab configuration directory.

### 🔖 1. 📂 Locate or Create the Configuration File

The configuration file is named `project-families.json` and must be placed in your global config directory:

- **Windows:** `C:\Users\<username>\.awlab-id\agent-memory\project-families.json`
- **Mac/Linux:** `~/.awlab-id/agent-memory/project-families.json`

### 🔖 2. 📝 Define the JSON Structure (v2 format)

Use the recommended `v2` format to map a unique `family_slug` to its member projects.

**Example `project-families.json`:**

```json
{
  "my_awesome_product": {
    "name": "My Awesome Product Suite",
    "members": [
      {
        "path": "D:/Project/IDE/my-frontend-app",
        "project_id": "frontend"
      },
      {
        "path": "D:/Project/IDE/my-backend-api",
        "project_id": "backend"
      }
    ]
  }
}
```

**Fields:**

- `family_slug` (e.g., `"my_awesome_product"`): The unique identifier for the family. Use lowercase, numbers, and underscores (no spaces).
- `name`: A human-readable name for the family.
- `members`: A list of the connected projects.
  - `path`: The absolute path to the project directory on your local machine. Use forward slashes (`/`).
  - `project_id`: A unique short name for the project (e.g., `"frontend"`). _Note: if the project already has an `.ai/project-id` file, that local ID will be automatically considered authoritative and will override the one declared here._

### 🔖 3. 🤖 Usage by AI Agents

Once this file is saved, no further setup is required from you.

When an AI agent interacts with any of these projects, it will dynamically detect the family relationship. The agent can then use tools like `graph_build` with `family="my_awesome_product"` to produce cross-repository insights, or use `mem_search` with `store="family_my_awesome_product"` to recall shared architectural conventions.

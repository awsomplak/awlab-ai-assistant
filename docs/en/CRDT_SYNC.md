# 📖 Cloud Memory Support (CRDT Sync)

> [🏠 README](../../README.md) · [📚 Docs](../../README.md#documentation) · **Cloud Memory Support (CRDT Sync)**

By default, AWLab-AI-Assistant's Memory Bank operates entirely offline using local SQLite (`agent-recall`). This ensures perfect project isolation, data privacy, and zero latency. However, for power users who want to sync their AI's memories across multiple devices or the cloud, we provide experimental support for **CRDT (Conflict-Free Replicated Data Types)** via the `cr-sqlite` extension.

---

## 📌 How it works

When the AWLab-AI-Assistant MCP server connects to the SQLite memory database, it actively checks for a compiled `cr-sqlite` extension. If the extension is found, it is injected directly into the SQLite connection.

This gives your local `memory.db` file CRDT capabilities, meaning it can be replicated and merged with other databases across the network without encountering synchronization conflicts.

> [!WARNING]
> AWLab-AI-Assistant handles the loading of the extension and making the database CRDT-aware. It **does not** ship with a background network synchronization daemon. You are responsible for running a sync peer (such as a generic `vlcn.io` sync server or a custom replication script) to actually transport the data to the cloud.

---

## 📌 Installation Guide

To enable CRDT support, you need to place the compiled `cr-sqlite` shared library in the correct configuration directory.

### 🔖 1. Download the Extension

Download the compiled `crsqlite` extension for your operating system from the official [vlcn.io releases page](https://github.com/vlcn-io/cr-sqlite/releases).

- **Windows:** `crsqlite.dll`
- **macOS:** `crsqlite.dylib`
- **Linux:** `crsqlite.so`

### 🔖 2. Place it in the Configuration Directory

Create an `extensions` folder inside the primary configuration directory (`~/.awlab-id/agent-memory/`) and place the downloaded file inside it.

```bash
# 📖 Example for Linux/macOS
mkdir -p ~/.awlab-id/agent-memory/extensions/
mv ~/Downloads/crsqlite.so ~/.awlab-id/agent-memory/extensions/crsqlite.so
```

> [!NOTE]
> Ensure the file is named exactly `crsqlite.dll`, `crsqlite.dylib`, or `crsqlite.so` depending on your OS. The system looks for this exact filename.

### 🔖 3. Restart the Server

Once the file is in place, simply restart your AI agent. The MCP server will automatically detect the extension, patch the SQLite connection, and enable `cr-sqlite`. You will see the extension loaded in the debug logs if you have logging enabled (`LOG_LEVEL=DEBUG`).

---

## 📌 Syncing to the Cloud

Once the extension is loaded, your `memory.db` file is ready to be synchronized.

You can use the official `vlcn.io` sync server or any other CRDT-compatible SQLite synchronization method to replicate the `~/.awlab-id/agent-memory/` directory (or specific project-isolated `.ai/memory-bank/` databases) to a central cloud server. Because the data is CRDT-aware, edits made on your laptop will cleanly merge with edits made on your desktop!

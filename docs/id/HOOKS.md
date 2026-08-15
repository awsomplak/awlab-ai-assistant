# Registrasi Hook (otomasi opsional)

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumen](../../README_ID.md#dokumentasi) · **Registrasi Hook**

Hook adalah fitur otomasi yang bersifat **opsional** dan penggunaannya *Tanpa Token* (zero-LLM) sebagai bagian dari fitur MCP server. Dengan hook, host agent atau IDE bisa menjalankan exe / binary hasil build (`dist/bin/awlab-ai-assistant{.exe}`) pada event lifecycle dari host agent atau IDE (penggunaan tool, saat user melakukan prompt, saat sesi berjalan, saat sesi berhenti atau selesai) sehingga observasi pola pengguna (user pattern) tertangkap otomatis — tanpa keterlibatan agent dan tanpa biaya LLM tambahan.

**Singkatnya: memasang MCP tanpa hook tetap berfungsi normal.** Hook hanya menambahkan penangkapan otomatis. Baca [pro/kontra](#pro--kontra-mengaktifkan-hook) di bawah ini untuk lebih detail.

---

## Apakah hook wajib? (Tidak)

| Mode | Pengambilan pola kebiasaan pengguna | Proses pengolahan tetap berjalan? |
|------|-------------------------------------|-----------------------------------|
| **MCP saja** (tanpa hook) | Agent akan mengambil pola kebiasaan pengguna melalui perintah `mem_observe` dalam penggunaan tool `action_call` | ✅ Ya — setiap `action_call` menjalankan *bake tick* secara *inline*, dan scheduler dari proses latar belakang melakukan *re-bake* pada workspace yang aktif |
| **MCP + hook** | Host (Agent atau IDE) menangkap event yang melakukan perintah secara otomatis (zero-LLM), ditambah `mem_observe` | ✅ Ya — penyimpanan yang sama, alur kerja yang sama |

**Kesimpulan:** server MCP adalah inti utamanya. Hook hanyalah fitur tambahan. Anda dapat menggunakan MCP saja dahulu dan menggunakan *hook* di kemudian hari tanpa perlu melakukan migrasi.

---

## Pro & Kontra mengaktifkan hook

| | Deskripsi |
|---|---|
| ✅ **Pro** | **Perekaman pola kebiasaan yang Tanpa Token (zero-LLM Capture)** — perintah yang dijalankan pengguna (contoh: `pnpm install`) dicatat langsung sebagai observasi tanpa menghabiskan token LLM. <br> **Otomatis & Selalu Aktif (Always-on)** — Proses perekaman tetap berjalan meskipun agent lupa memanggil fungsi `mem_observe`. <br> **Pemrosesan Otomatis di Akhir Sesi (Turn-end Baking)** — event `Stop` akan otomatis memproses / mengolah (***bake***) data yang terkumpul. <br> **Injeksi Konteks (Context Injection)** — saat prompt dapat menyuntikkan pola data yang sudah diproses (*baked patterns*) sesuai scope ke dalam konteks. <br> **Aman dari Perulangan Tak Terbatas (Self-loop Safe)** — desain anti-loop: saat prompt hanya bertugas menyuntikkan data (injection), sedangkan tool hanya bertugas untuk mencatat hasilnya saja. |
| ⚠️ **Kontra** | **Konfigurasi per-host agent atau IDE** — perlu melakukan pengaturan hook sekali namun berlaku untuk setiap agent atau IDE (lihat di bawah). **Subproses per event** — Setiap kali hook aktif, sistem akan menjalankan file .exe atau binary (tergantung OS) satu kali (ada sedikit overhead durasi pemanggilan awal yang disebabkan oleh PyInstaller pada setiap tool call). **Perekaman selektif** — hanya event dari tool yang membawa perintah saja yang dicatat sebagai observasi. Tool yang membaca file dan prompt tidak dicatat. **Pembacaan projct path** — host agent atau IDE yang payload-nya tidak memiliki konteks project path memerlukan parameter `--project <path>` atau variable environment khusus (contoh: variable environment `CLAUDE_PROJECT_DIR` pada claude code). |

---

## Prasyarat

1. Executable hasil build: `python scripts/run.py build` → `dist/bin/awlab-ai-assistant{.exe}`.
2. Konfigurasi registrasi siap pakai (setiap build) di `dist/profiles/hooks/`:
   `claude.hooks.json`, `hermes.hooks.yaml`, `copilot.hooks.txt`, `cline.hooks.txt`.

> Perintah hook memakai exe atau binary yang sama dengan server MCP — tidak perlu instalasi lainnya.

---

## Fungsi tiap event

Event dibedakan berdasarkan `jenis` yang menentukan perilakunya:

| Jenis | Contoh event host | Perilaku |
|------|------------------------|-----------|
| `prompt` | `UserPromptSubmit`, `pre_llm_call` | menyisipkan pola siap pakai (*baked patterns*) dengan cakupan *stack* |
| `tool` | `PostToolUse`, `post_tool_call` | **CAPTURE** — menambahkan observasi saat tool membawa perintah (*command*) |
| `pre_tool` | `PreToolUse` | **pemeriksaan penyimpangan** — izinkan/blokir berdasarkan pola tersimpan |
| `stop` | `Stop` | **BAKE** — menjalankan pipeline (key → hitung → consistency → confidence) |
| `session` / `subagent` | `SessionStart`, `SubagentStop` | hanya-observer (belum ada aksi) |

Proses perekaman data pola kebiasaan atau pattern bersifat **selektif** contoh: sebuah *tool* Bash dengan `{"command": "pnpm install"}` akan dicatat sebagai sebuah observasi, sedangkan *tool* untuk melakukan pembacaan (tanpa perintah) tidak ditandai sebagai observasi — karena membaca berkas bukanlah sebuah kebiasaan atau pattern.

---

## Registrasi per-Agent

### 1) Claude Code

Gabungkan blok `hooks` dari `dist/profiles/hooks/claude.hooks.json` ke `~/.claude/settings.json` (buat jika belum ada). Ganti `awlab-ai-assistant.exe` dengan path (lokasi) file exe atau binary hasil build Anda:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event UserPromptSubmit" }] }
    ],
    "PostToolUse": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event PostToolUse" }] }
    ],
    "PreToolUse": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event PreToolUse" }] }
    ],
    "SubagentStop": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event SubagentStop" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event Stop" }] }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant.exe hook --agent claude --event SessionStart" }] }
    ]
  }
}
```

Claude Code menentukan project dari payload (`cwd`) atau environment `$CLAUDE_PROJECT_DIR`.

### 2) Hermes

Gabungkan blok `hooks:` dari `dist/profiles/hooks/hermes.hooks.yaml` ke konfigurasi Hermes:

```yaml
hooks:
  pre_llm_call:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event pre_llm_call"
  post_tool_call:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event post_tool_call"
  pre_tool_call:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event pre_tool_call"
  subagent_stop:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event subagent_stop"
  on_session_start:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event on_session_start"
  on_session_end:
    - command: "D:\\path\\to\\awlab-ai-assistant.exe hook --agent hermes --event on_session_end"
```

### 3) Cline

Hook Cline didaftarkan di UI pengaturan (pengaturan MCP/hook). Tambahkan perintah dari `dist/profiles/hooks/cline.hooks.txt`:

```
awlab-ai-assistant.exe hook --agent cline --event NewTask
awlab-ai-assistant.exe hook --agent cline --event PostToolUse
awlab-ai-assistant.exe hook --agent cline --event Stop
```

### 4) VSCode Copilot

Copilot tidak membaca file konfigurasi hook melainkan registrasinya melalui pengaturan/UI VSCode itu sendiri. Gunakan perintah dari `dist/profiles/hooks/copilot.hooks.txt`:

```
awlab-ai-assistant.exe hook --agent copilot --event user-prompt-submit
awlab-ai-assistant.exe hook --agent copilot --event post-tool-use
awlab-ai-assistant.exe hook --agent copilot --event session-start
awlab-ai-assistant.exe hook --agent copilot --event session-end
awlab-ai-assistant.exe hook --agent copilot --event subagent-stop
awlab-ai-assistant.exe hook --agent copilot --event stop
```

---

## Verifikasi hook berfungsi

**Manual** (di Linux/macOS gunakan `printf`, di Windows gunakan `cmd /c "echo ... | exe hook ..."` atau
skrip kustom — catatan: `|` PowerShell bisa tidak andal untuk stdin native):

```bash
# capture path (tool event with a command)
echo '{"tool_name":"Bash","tool_input":{"command":"pnpm install"}}' | \
  awlab-ai-assistant.exe hook --agent claude --event PostToolUse --project /path/to/project
# → writes /path/to/project/.ai/memory-bank/observations.jsonl
# → stdout: {}

# prompt path (READ)
echo '{"prompt":"please run the tests"}' | \
  awlab-ai-assistant.exe hook --agent claude --event UserPromptSubmit --project /path/to/project
# → stdout: {"decision":"allow"}
```

**Otomatis**: `python scripts/live_probe.py` (dari repositori ini) menyertakan pemeriksaan penangkapan hook — jika outputnya exit 0 / `35 passed` berarti hook berhasil menjalankan dan menyimpan observasi dari awal hingga akhir dalam 35 sesi percobaan.

---

## Pemecahan masalah

| Masalah | Penyebab / solusi |
|---------|-------------|
| Hook berjalan (exit 0) tapi tidak ada observasi | Event-nya berupa prompt, pembacaan file (file read), atau tool yang tidak memiliki perintah (memang di desain demikian). Gunakan event tool yang membawa perintah (*comman-carrying*), atau event `Stop` untuk memproses (*bake*) data. |
| Tidak ada observasi dan `.ai/project-id` tidak dibuat | Payload tidak pernah sampai ke proses — periksa hasil output stdin (jika menggunakan PowerShell karakter `\|` tidak stabil, jika tetap ingin menggunakan PowerShell gunakan redireksi via `subprocess`/`cmd`) serta periksa kembali lokasi file exe atau binary apakah sudah benar path atau lokasi file nya. |
| Project tidak terdeteksi | Tambahkan argumen `--project <path>`, atau pastikan payload dari agent punya `cwd` / `CLAUDE_PROJECT_DIR`. |
| Hook tidak merespon apapun | Lokasi file exe atau binary berubah sehingga tidak sama dengan yang didaftarkan — konfigurasi ulang dan arahkan kembali ke lokasi dimana file exe atau binary berada, contoh: `dist/bin/awlab-ai-assistant{.exe}`. |
| Observasi duplikat tidak bertambah | Proteksi duplikasi (dedup/delta) sedang berjalan atau belum selesai — input yang identik tidak akan terhitung dua kali (double-counted). |

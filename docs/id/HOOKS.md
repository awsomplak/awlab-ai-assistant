# 📖 Registrasi Hook (Otomasi Opsional)

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumen](../../README_ID.md#dokumentasi) · **Registrasi Hook**

Hook adalah fitur otomasi yang bersifat **opsional** dan penggunaannya *Zero-Token* (zero-LLM) sebagai bagian dari fitur server MCP. Dengan hook, AI agent atau IDE dapat menjalankan *executable binary* **bridge** yang telah di-*publish* (`~/.awlab-id/agent-memory/bin/awlab-ai-assistant`) pada setiap kejadian siklus hidup (lifecycle event) agent atau IDE tersebut (seperti penggunaan tool, pengiriman prompt, dimulainya sesi, hingga selesainya sesi). Hal ini membuat observasi pola pengguna (user pattern) dapat terekam secara otomatis — tanpa harus melibatkan agen secara eksplisit dan tanpa biaya LLM tambahan. Untuk mode `hook`, bridge akan me-`exec` worker (`awlab-ai-worker`) dengan argumen (argv) yang sama, sehingga setiap pemicu hanya berjalan sebentar dalam satu proses tunggal.

**Singkatnya: MCP tanpa hook akan tetap berfungsi normal.** Hook hanya sekadar menambahkan perekaman pola secara otomatis. Baca [pro/kontra](#pro--kontra-mengaktifkan-hook) di bawah ini untuk lebih detail.

---

## 📌 Apakah hook wajib? (Tidak)

| Mode | Pengambilan pola pengguna | Proses Bake (pengolahan data) tetap berjalan? |
|------|-------------------------------------|-----------------------------------|
| **MCP saja** (tanpa hook) | Agent akan menangkap pola pengguna secara manual melalui pemanggilan perintah `mem_observe` dengan tool `action_call` | ✅ Ya — setiap `action_call` menjalankan *bake tick* secara *inline*, dan *scheduler* latar belakang akan melakukan *re-bake* pada workspace yang aktif |
| **MCP + hook** | Agent / IDE host merekam *event* (kejadian tool/prompt) secara otomatis (zero-LLM), dan ditambah dengan panggilan `mem_observe` | ✅ Ya — menggunakan penyimpanan dan alur kerja yang sama |

**Kesimpulan:** Server MCP adalah inti dari sistem ini. Hook hanyalah fitur tambahan. Anda dapat menggunakan MCP-nya saja pada awalnya, lalu menambahkan hook di kemudian hari tanpa perlu repot melakukan migrasi.

---

## 📌 Pro & Kontra mengaktifkan hook

| | Deskripsi |
|---|---|
| ✅ **Pro** | **Perekaman Pola Zero-Token (*Zero-LLM Capture*)** — perintah-perintah yang dijalankan pengguna (contoh: `pnpm install`) langsung dicatat sebagai observasi tanpa menghabiskan token LLM. <br> **Otomatis & Selalu Aktif (*Always-on*)** — Perekaman berjalan tanpa henti meskipun agent lupa memanggil tool `mem_observe`. <br> **Pemrosesan Otomatis di Akhir Sesi (*Turn-end Baking*)** — event `Stop` akan otomatis mengolah (*bake*) data pola-pola yang terkumpul. <br> **Injeksi Konteks (*Context Injection*)** — pada saat prompt, sistem otomatis menyuntikkan data pola yang sudah jadi (*baked patterns*) berdasarkan cakupannya (scope) ke dalam konteks prompt. <br> **Aman dari Loop Tak Terbatas (*Self-loop Safe*)** — desainnya kebal terhadap perulangan terus-menerus: pada saat prompt, sistem hanya bertugas menyuntikkan data, sedangkan tool hanya bertugas merekam hasilnya. |
| ⚠️ **Kontra** | **Konfigurasi per-Agent/IDE** — Anda harus melakukan pengaturan registrasi hook ini satu kali per setiap AI agent (lihat di bawah). **Sub-proses per event** — Setiap kali hook aktif, sistem memuat *bridge* lalu me-`exec` worker (terdapat sedikit latensi yang diakibatkan oleh inisialisasi PyInstaller pada setiap *tool call*). **Perekaman selektif** — Hanya event tool yang membawa perintah operasi yang dicatat; operasi membaca file atau mengirim *prompt* biasa tidak dicatat. **Pendeteksian Path Project** — AI agent yang muatan payload-nya tidak menyertakan context lokasi project akan butuh argumen tambahan `--project <path>` atau sebuah environment variable (contoh: `$CLAUDE_PROJECT_DIR` pada Claude Code). |

---

## 📌 Prasyarat

1. Pasangan *executable* yang telah di-*publish*: `python scripts/run.py publish --target=binary` → `~/.awlab-id/agent-memory/bin/awlab-ai-assistant` (bridge) + `~/.awlab-id/agent-memory/bin/awlab-ai-worker` (worker).
2. Konfigurasi registrasi siap pakai yang dihasilkan (setiap build) di folder `dist/profiles/hooks/`:
   `claude.hooks.json`, `hermes.hooks.yaml`, `copilot.hooks.txt`, `cline.hooks.txt`.

> Hook memanggil *bridge* `awlab-ai-assistant` yang sama persis seperti yang digunakan server MCP. Pada mode `hook`,
> *bridge* akan menunggu hingga proses `.update_lock` yang sedang berjalan selesai (sehingga hook tidak akan
> crash jika binary sedang diperbarui), kemudian langsung mengeksekusi worker dengan argumen yang
> sama. Tidak dibutuhkan instalasi *daemon* atau *library* lain.

---

## 📌 Fungsi tiap event

Event dibedakan berdasarkan `jenis` (*type*) yang akan menentukan perilakunya:

| Jenis | Contoh event host | Perilaku |
|------|------------------------|-----------|
| `prompt` | `UserPromptSubmit`, `pre_llm_call` | menyisipkan pola-pola jadi (*baked patterns*) dengan cakupan *stack* saat ini |
| `tool` | `PostToolUse`, `post_tool_call` | **CAPTURE** — menambahkan observasi baru ketika suatu tool membawa operasi perintah (*command*) |
| `pre_tool` | `PreToolUse` | **Pemeriksaan aturan** — mencegah/mengizinkan tool berjalan sesuai dengan pola yang ada |
| `stop` | `Stop` | **BAKE** — menjalankan pipeline pengolahan pola (key → iterasi → konsistensi → confidence) |
| `session` / `subagent` | `SessionStart`, `SubagentStop` | hanya observasi pasif (belum diimplementasikan tindakannya) |

Proses penangkapan observasi bersifat **selektif**. Contoh: penggunaan tool Bash dengan `{"command": "pnpm install"}` akan direkam sebagai observasi, sedangkan tool untuk membaca file tidak akan direkam — karena sekadar membaca file bukanlah sebuah "pola kebiasaan" (*user pattern*).

---

## 📌 Registrasi per-Agent

### 🔖 1) Claude Code

Gabungkan blok `hooks` dari file `dist/profiles/hooks/claude.hooks.json` ke dalam `~/.claude/settings.json` (buat file ini jika belum ada). Pastikan untuk mengganti path ke `awlab-ai-assistant` dengan path *binary executable* asli Anda:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant hook --agent claude --event UserPromptSubmit" }] }
    ],
    "PostToolUse": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant hook --agent claude --event PostToolUse" }] }
    ],
    "PreToolUse": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant hook --agent claude --event PreToolUse" }] }
    ],
    "SubagentStop": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant hook --agent claude --event SubagentStop" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant hook --agent claude --event Stop" }] }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "D:\\path\\to\\awlab-ai-assistant hook --agent claude --event SessionStart" }] }
    ]
  }
}
```

Claude Code sudah menentukan path project otomatis dari field `cwd` di payload-nya atau dari variabel environment `$CLAUDE_PROJECT_DIR`.

### 🔖 2) Hermes

Gabungkan blok `hooks:` dari file `dist/profiles/hooks/hermes.hooks.yaml` ke dalam konfigurasi Hermes milik Anda:

```yaml
hooks:
  pre_llm_call:
    - command: "D:\\path\\to\\awlab-ai-assistant hook --agent hermes --event pre_llm_call"
  post_tool_call:
    - command: "D:\\path\\to\\awlab-ai-assistant hook --agent hermes --event post_tool_call"
  pre_tool_call:
    - command: "D:\\path\\to\\awlab-ai-assistant hook --agent hermes --event pre_tool_call"
  subagent_stop:
    - command: "D:\\path\\to\\awlab-ai-assistant hook --agent hermes --event subagent_stop"
  on_session_start:
    - command: "D:\\path\\to\\awlab-ai-assistant hook --agent hermes --event on_session_start"
  on_session_end:
    - command: "D:\\path\\to\\awlab-ai-assistant hook --agent hermes --event on_session_end"
```

### 🔖 3) Cline

Hook untuk Cline perlu didaftarkan di halaman pengaturan UI extension-nya (pada tab pengaturan MCP/hook). Salin teks perintah yang ada di `dist/profiles/hooks/cline.hooks.txt`:

```
awlab-ai-assistant hook --agent cline --event NewTask
awlab-ai-assistant hook --agent cline --event PostToolUse
awlab-ai-assistant hook --agent cline --event Stop
```

### 🔖 4) VSCode Copilot

Copilot tidak mengandalkan file pengaturan hook berformat teks, tetapi diregistrasikan dari antarmuka Settings VSCode itu sendiri. Tambahkan instruksi pemanggilan hook seperti di `dist/profiles/hooks/copilot.hooks.txt`:

```
awlab-ai-assistant hook --agent copilot --event user-prompt-submit
awlab-ai-assistant hook --agent copilot --event post-tool-use
awlab-ai-assistant hook --agent copilot --event session-start
awlab-ai-assistant hook --agent copilot --event session-end
awlab-ai-assistant hook --agent copilot --event subagent-stop
awlab-ai-assistant hook --agent copilot --event stop
```

### 🔖 5) Google Antigravity & Antigravity IDE

Gabungkan blok `AWLab-AI-Assistant` dari file `dist/profiles/hooks/antigravity.hooks.json` ke file konfigurasi `~/.gemini/config/hooks.json` (atau `.agents/hooks.json`). Gantilah teks instruksi menjadi *path absolut executable*:

```json
{
  "AWLab-AI-Assistant": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "awlab-ai-assistant hook --agent antigravity --event PreToolUse"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "awlab-ai-assistant hook --agent antigravity --event PostToolUse"
          }
        ]
      }
    ],
    "PreInvocation": [
      {
        "type": "command",
        "command": "awlab-ai-assistant hook --agent antigravity --event PreInvocation"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "awlab-ai-assistant hook --agent antigravity --event Stop"
      }
    ]
  }
}
```

---

## 📌 Verifikasi hook berfungsi

**Manual** (di Linux/macOS jalankan `printf` atau `echo`, di Windows gunakan `cmd /c "echo ... | exe hook ..."` — catatan: fitur *pipe* `|` PowerShell kadang tidak bekerja dengan stabil untuk aplikasi *native console*):

```bash
# 📖 penangkapan tool yang menjalankan perintah
echo '{"tool_name":"Bash","tool_input":{"command":"pnpm install"}}' | \
  awlab-ai-assistant hook --agent claude --event PostToolUse --project /path/to/project
# → menyimpan ke /path/to/project/.ai/memory-bank/observations.jsonl
# → stdout: {}

# 📖 penyuntikan context ke prompt (BACA)
echo '{"prompt":"please run the tests"}' | \
  awlab-ai-assistant hook --agent claude --event UserPromptSubmit --project /path/to/project
# → stdout: {"decision":"allow"}
```

**Otomatis**: Anda dapat menjalankan `python scripts/live_probe.py` dari repository ini, yang sudah mencakup simulasi penangkapan hook. Jika script mengeluarkan exit 0 / `35 passed`, maka konfigurasi hook bekerja dengan baik dari awal sampai akhir sesi.

---

## 📌 Pemecahan masalah

| Kendala | Penyebab / Solusi |
|---------|-------------|
| Hook berjalan (exit 0) tapi tidak ada data observasi | Event-nya mungkin adalah prompt biasa, pembacaan file, atau penggunaan tool yang tak membawa argumen perintah (ini desain yang disengaja). Cobalah event tool yang memuat perintah aktual (*command*), atau picu event `Stop` untuk melakukan *bake*. |
| Tidak ada observasi dan `.ai/project-id` tidak pernah dibuat | Payload (data) tidak pernah berhasil masuk ke *bridge* — periksa ulang mekanisme `stdin` OS Anda (karakter `\|` pada PowerShell bermasalah; gunakan PowerShell dengan operator spesifik atau alihkan eksekusinya via bash/cmd). Juga, pastikan *path binary executable* benar. |
| Project tidak dikenali atau terdeteksi | Sertakan argumen `--project <path>`, atau pastikan format payload agent Anda mengirim field direktori kerja `cwd` atau environment variabel `$CLAUDE_PROJECT_DIR`. |
| Hook seolah tidak bereaksi (diam saja) | *Path* ke file *binary* mungkin tidak akurat atau rusak. Cek apakah alamat di dalam file `.json` mengarah langsung ke `~/.awlab-id/agent-memory/bin/awlab-ai-assistant`. |
| Data observasi tidak bertambah biarpun event terjadi | Fitur proteksi duplikat (deduplikasi data delta) mungkin menolaknya — dua perintah identik beruntun tidak akan dihitung dua kali secara redundan (*double-counted*). |

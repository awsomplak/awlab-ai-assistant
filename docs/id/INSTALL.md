# 📖 Instal & Terapkan

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumen](../../README_ID.md#dokumentasi) · **Instal & Terapkan**

Panduan ini mencakup semua hal yang Anda butuhkan untuk menjalankan AWLab-AI-Assistant di project Anda dan menghubungkannya ke AI agent:

1. [Melakukan clone repositori](#1-clone-repositori)
2. [Instalasi virtual environment Python dan dependensi server MCP](#2-instalasi-python-venv-dan-dependensi-server-mcp)
3. [Build binary executable](#3-build-binary-executable)
4. [Publikasikan rules & skill ke agent Anda](#4-publikasikan-rules--skill-ke-agent-anda)
5. [Menyambungkan MCP](#5-sambungkan-server-mcp)
6. [Gunakan di AI Agent Anda](#6-gunakan-di-agent-anda)
7. [Verifikasi pemasangan](#7-verifikasi-pemasangan)
8. [Environment variabel & konfigurasi](#8-environment-variabel--konfigurasi)
9. [Referensi CLI](#9-referensi-cli)
10. [Pemecahan masalah](#10-pemecahan-masalah)

---

## 📌 Struktur Project

Tata letak repository:

```
{root-project}/
├── assets/
│   ├── agents/                  # 1 sub-agent
│   ├── rules/                   # 14 file rules (sumber)
│   ├── skills/                  # 5 file skill (sumber)
│   └── workflows/               # tempat custom workflow Anda (1 default bawaan)
├── dist/
│   └── profiles/                # Output profil yang telah dikompilasi per-agent
├── src/mcp_server/              # Source code server MCP berbasis Python
├── scripts/
│   ├── run.py                   # CLI utama untuk proses build & development
│   └── stop-mcp-servers.ps1     # Script helper untuk menghentikan paksa semua server MCP `awlab-*` (khusus Windows PowerShell)
├── tests/                       # Pytest suite (360 tes)
├── docs/                        # Halaman dokumentasi lainnya
├── CHANGELOG.md
└── pyproject.toml
```

---

## 📌 Kebutuhan Instalasi

- **Python 3.10+** (dibutuhkan untuk build server MCP dan wajib sudah terinstal sebelumnya)
- **agent-recall** (sebagai backend _Memory Bank_ / _Code Graph_)
- **graphify** (untuk mengindeks code base ke dalam _Code Graph_)
- **AI Model** yang sudah mendukung _tool use_
- Salah satu dari: **Cline**, **VS Code Copilot**, **Claude Code**, **Hermes Agent**, **OpenCode**, atau **Google Antigravity / Antigravity IDE**

**Saran kebutuhan LLM model:** 🟢 Sederhana → lokal 1.5B–3B · 🟡 Menengah → lokal 14B–32B · 🔴 Kompleks → frontier (Claude, GPT)

---

## 📌 1. Clone repositori

```bash
# 📖 Clone repository
git clone https://github.com/awsomplak/awlab-ai-assistant.git

# 📖 Masuk ke dalam direktori hasil clone
cd AWLab-AI-Assistant
```

## 📌 2. Instalasi python-venv dan dependensi server MCP

> ⚠️ **Sesuaikan dengan sistem operasi (OS) Anda.** Perintah aktivasi _virtual environment_ Python berbeda antara Windows dan Linux/macOS — menyalin perintah yang salah akan menyebabkan error.

### 🔖 Aktivasi virtual environment Python

#### Windows (PowerShell)

```powershell
# 📖 Buat virtual environment
python -m venv .venv

# 📖 Aktifkan virtual environment di PowerShell
.venv\Scripts\Activate.ps1

# 📖 Jika menggunakan CMD, gunakan perintah berikut
.venv\Scripts\activate.bat
```

#### Linux / macOS

```bash
# 📖 Buat virtual environment
python -m venv .venv

# 📖 Aktifkan virtual environment di terminal
source .venv/bin/activate
```

### 🔖 Instalasi dependensi Python

```bash
# 📖 Pastikan virtual environment sudah aktif
#
# 📖 Instalasi dependensi standar (untuk pemakaian langsung)
pip install -e .

# 📖 atau
# 📖 Instalasi dependensi tambahan untuk development/testing (opsional)
pip install -e ".[dev]"
```

---

## 📌 3. Build binary executable

```bash
# 📖 Build executable untuk sistem operasi Anda saat ini
python scripts/run.py build

# 📖 Build executable untuk target platform tertentu
python scripts/run.py build --target-os=linux
python scripts/run.py build --target-os=all
```

Hasil build akan berada di folder `dist/bin/`:

| Binary               | Peran                                                                                                                                                     | Tool MCP yang Tersedia                      |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| `awlab-ai-assistant` | **Bridge** — _proxy stdio_ yang menjaga _pipe_ JSON-RPC IDE tetap hidup. Berfungsi sebagai _entrypoint_ tunggal yang dipakai semua konfigurasi agent/IDE. | `action_call` (dispatcher), `action_help`   |
| `awlab-ai-worker`    | **Worker** — Server MCP inti yang berat (ONEDIR); dijalankan & dikelola oleh _bridge_, mendukung fitur _hot-swap_ saat Anda melakukan `publish`.          | _(2 tool yang sama, dilayani lewat bridge)_ |

Mengapa arsitekturnya dipecah dua? Memperbarui _binary file_ yang sedang digunakan akan mengharuskan proses MCP dihentikan secara paksa, hal ini memutus koneksi JSON-RPC sehingga muncul _error_ `context canceled` di IDE. Dengan memisahkan **bridge + worker**, proses `publish` dapat bekerja sebagai berikut:

1. menulis file `.update_lock` di direktori bin hasil publish,
2. mematikan hanya proses `awlab-ai-worker` (sementara _bridge_ tetap menyala),
3. menimpa/mengganti kedua binary dengan yang baru,
4. menghapus `.update_lock`.

Setelah proses itu, _bridge_ yang sedang aktif akan otomatis menyalakan _worker_ versi baru dan melanjutkan antrean _request_ — sehingga pengguna/IDE hanya merasakan sedikit jeda waktu tanpa terkena _error_ `context canceled`. Pendekatan ini aman untuk multiple IDE yang berjalan bersamaan (setiap IDE mendapat pasangan bridge → worker sendiri yang dikelola lewat satu `.update_lock`), dan karena nama aplikasinya tetap `awlab-ai-assistant`, maka **Anda tidak perlu memperbarui konfigurasi di sisi agent atau IDE**.

> **Tips:** Untuk pengembangan lokal (_local development_), Anda bisa menjalankan server ini langsung dari source code (cukup `pip install -e .` dan jalankan command `AWLab-AI-Assistant`) — membangun executable hanya diperlukan untuk keperluan _production_.

### 🔖 Publikasikan binary (production)

Perintah `build` di atas akan membuat binary di direktori `dist/bin/` — direktori tersebut hanyalah **lokasi output build sementara**. Untuk menyebarkannya (deploy) ke **lokasi publikasi utama** yang nantinya digunakan oleh IDE/agent Anda, jalankan:

```bash
python scripts/run.py publish --target=binary
```

Perintah ini memicu _hot-reload deployment_ ke direktori operasional `~/.awlab-id/agent-memory/bin/`:

| Lokasi publikasi (macOS/Linux)                    | Padanan di Windows                                                |
| ------------------------------------------------- | ----------------------------------------------------------------- |
| `~/.awlab-id/agent-memory/bin/awlab-ai-assistant` | `%USERPROFILE%\.awlab-id\agent-memory\bin\awlab-ai-assistant.exe` |

> 💡 Khusus pengguna Windows: variabel `%USERPROFILE%` hanya bisa dibaca otomatis oleh Command Prompt (CMD) atau PowerShell. Dalam file konfigurasi berbasis JSON atau YAML (seperti pengaturan IDE), Anda wajib menggunakan _path absolut_ secara lengkap, contoh: `C:\Users\<nama-anda>\.awlab-id\agent-memory\bin\awlab-ai-assistant.exe`.

Sistem _publish_ dirancang dengan fitur **hot-reload aware**: ia akan mengaktifkan lock, mematikan proses `awlab-ai-worker`, menimpa executable dengan yang baru, dan langsung mencabut lock. Hal ini membuat pembaruan berlangsung sangat mulus. Pastikan konfigurasi agent/IDE Anda selalu menunjuk ke lokasi publikasi utama (`~/.awlab-id/...`), **bukan** ke folder output build `dist/bin/`.

---

## 📌 4. Publikasikan rules & skill ke agent Anda

AWLab-AI-Assistant memiliki **14 buah rules** dan **5 buah skills** bawaan di folder `assets/`. Melalui fungsi `publish`, Anda dapat menyalin profil terkompilasi ini secara otomatis ke dalam direktori spesifik dari AI agent yang Anda gunakan — fungsi ini hanya perlu dijalankan **satu kali saja** per agent. **SANGAT DISARANKAN** untuk mencadangkan (backup) konfigurasi lama Anda jika Anda memilikinya, lihat [Target publikasi](#target-publikasi).

```bash
# 📖 Publikasikan executable utama (bridge + worker) → ~/.awlab-id/agent-memory/bin/
python scripts/run.py publish --target=binary

# 📖 Publikasikan konfigurasi profil ke AI agent spesifik
python scripts/run.py publish --target=cline        # Cline
python scripts/run.py publish --target=copilot      # VS Code Copilot
python scripts/run.py publish --target=claude       # Claude Code
python scripts/run.py publish --target=hermes       # Hermes Agent
python scripts/run.py publish --target=opencode     # OpenCode
python scripts/run.py publish --target=antigravity  # Google Antigravity / Antigravity IDE
python scripts/run.py publish --target=all          # Publikasikan ke semua agent yang terdeteksi

# 📖 Hapus profil yang telah terpasang (Uninstall)
python scripts/run.py publish --uninstall
python scripts/run.py publish --uninstall --target=copilot
```

### 🔖 Target publikasi

| Target        | Profil Rules                     | Output Lokasi Skills            |
| ------------- | -------------------------------- | ------------------------------- |
| `binary`      | — (executable bridge + worker)   | `~/.awlab-id/agent-memory/bin/` |
| `cline`       | `~/Documents/Cline/Rules/`       | `~/.agents/skills/`             |
| `copilot`     | `~/.copilot/instructions/`       | `~/.agents/skills/`             |
| `claude`      | `~/.claude/CLAUDE.md`            | `~/.claude/skills/`             |
| `hermes`      | — (tergabung dalam bentuk skill) | `~/.hermes/skills/`             |
| `opencode`    | `~/.config/opencode/AGENTS.md`   | `~/.config/opencode/skills/`    |
| `antigravity` | `~/.gemini/config/rules/`        | `~/.gemini/config/skills/`      |

> 💡 Jika Anda melewatkan langkah ke-3 ([Build binary executable](#3-build-binary-executable)), perintah `publish` akan tetap secara otomatis melakukan kompilasi rules/skill ke folder `dist/` sebelum menyebarkannya.
> Langkah publikasi profil ini cukup dilakukan sekali. Setelah profil agen siap, sambungkan server MCP seperti yang dijelaskan pada poin berikutnya (§5 - [Sambungkan server MCP](#5-sambungkan-server-mcp)).

---

## 📌 5. Sambungkan server MCP

Server MCP `AWLab-AI-Assistant` mengekspos **2 buah tool**: `action_call` dan `action_help` (lihat rinciannya di [Tool MCP yang Tersedia](AVAILABLE_TOOLS.md)). Untuk menghubungkan server ini, Anda cukup membuat **satu entri MCP Server baru** pada konfigurasi AI agent yang Anda pakai. Arahkan entri _command_ (perintah eksekusi)-nya menuju _path publikasi utama_ (`~/.awlab-id/agent-memory/bin/awlab-ai-assistant`):

```json
{
  "mcpServers": {
    "AWLab-AI-Assistant": {
      "type": "stdio",
      "command": "~/.awlab-id/agent-memory/bin/awlab-ai-assistant",
      "args": [],
      "env": {
        "LOG_ENABLED": "true",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

> ⚠️ **Catatan penting untuk pengguna Windows:** Karakter tilde (`~`) merupakan _shortcut shell bash_ dan **tidak** akan dikenali atau dikembangkan secara otomatis di dalam konfigurasi berformat JSON/YAML. Anda wajib menulis seluruh lokasi path profil user secara literal. Contoh: `C:\Users\<nama-anda>\.awlab-id\agent-memory\bin\awlab-ai-assistant.exe`.

> Jika konfigurasi di atas sudah berhasil, AI agent kini dapat memanfaatkan tool `action_call`.
> Untuk menambahkan kapabilitas ekstra berupa **perekaman data pola pengguna otomatis secara Zero-Token (_zero-LLM_)** pada setiap event siklus (contoh: _tool use_, _prompt_, _stop_), Anda bisa mengonfigurasi fitur **Hook**. Penggunaan Hook ini **tidak wajib** dan sepenuhnya opsional. Silakan baca dokumen [Registrasi Hook](HOOKS.md) jika tertarik memasangnya.

Untuk pemasangannya, salinlah konfigurasi JSON/YAML di atas ke pengaturan `mcpServers` dari masing-masing agent, kemudian _restart_ agent IDE/chat-nya:

### 🔖 Cline

Tambahkan block konfigurasi tadi melalui menu: **Cline Settings → MCP Servers → Edit JSON**.

### 🔖 VS Code Copilot

Sisipkan konfigurasinya ke dalam file `.vscode/mcp.json` di dalam folder project Anda, atau gunakan menu **Command Palette → MCP**.

### 🔖 Claude Code

Jalankan perintah penambahan di terminal secara langsung:

```bash
claude mcp add AWLab-AI-Assistant -- ~/.awlab-id/agent-memory/bin/awlab-ai-assistant
```

### 🔖 Hermes Agent

Tambahkan di dalam blok `mcp_servers:` pada file konfigurasi `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  AWLab-AI-Assistant:
    command: ~/.awlab-id/agent-memory/bin/awlab-ai-assistant
    args: []
    env:
      LOG_ENABLED: "true"
      LOG_LEVEL: INFO
```

### 🔖 OpenCode

Sisipkan konfigurasinya ke dalam blok `mcp` di dalam file `~/.config/opencode/opencode.json` (Perhatikan bahwa OpenCode menggunakan root key bernama `mcp`, bukan `mcpServers`):

```json
{
  "mcp": {
    "AWLab-AI-Assistant": {
      "type": "local",
      "command": ["~/.awlab-id/agent-memory/bin/awlab-ai-assistant"],
      "enabled": true
    }
  }
}
```

### 🔖 Google Antigravity & Antigravity IDE

Tambahkan key _object_ `AWLab-AI-Assistant` ke file `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "AWLab-AI-Assistant": {
      "command": "~/.awlab-id/agent-memory/bin/awlab-ai-assistant",
      "args": []
    }
  }
}
```

---

## 📌 6. Gunakan di AI Agent Anda

Setelah server tersambung, Anda bisa memberi perintah seperti biasa atau menggunakan fitur _slash command_ bawaan:

### 🔖 Penggunaan instruksi dalam prompt biasa

- _"follow rules"_ → memerintahkan AI agent untuk memuat data _Memory Bank_ dan daftar instruksi agar senantiasa dipatuhi.
- _"create plan"_ → meminta AI untuk membuat _plan_ rancangan implementasi baru. AI akan menuliskannya di file `plan.md` dan memecahnya menjadi daftar cek (checklist) tugas ke file `tasks.md`.
- _"start phase 1"_ → menyuruh AI agent untuk segera mengeksekusi tugas pertama yang tertulis di `tasks.md`.

### 🔖 Penggunaan slash command

- `/create plan` → cara ringkas untuk membuat rencana (plan) dan _checklist_ implementasi.
- `/plan-status` → memeriksa apakah status implementasi project sedang berjalan, dihentikan sementara (paused), atau telah selesai sepenuhnya.
- `/retrospective` → membuat dokumen ringkasan (review) atas pekerjaan implementasi fitur yang telah diselesaikan untuk disimpan di memori jangka panjang.

---

## 📌 7. Verifikasi pemasangan

1. **Server aktif** — panggil aksi `util_info` untuk memastikan apakah versi build dan agent membalas dengan akurat:

   ```
   action_call(action="util_info")
   ```

2. **Project-ID terpasang** — pada balasan awal, AI agent secara spesifik akan mencari ID project untuk memastikan pembatasan cakupan memori (_memory isolation_). Pastikan file identifikasi di `.ai/project-id` sudah tersedia. Jika belum terbuat otomatis, buat manual file teks biasa di `.ai/project-id` dan tuliskan ID nama project Anda di dalamnya.
3. **MCP aktif** — minta AI agent menjalankan instruksi aksi `action_help` untuk membaca ke-23 opsi perintah yang dibawanya.

---

## 📌 8. Environment variabel & konfigurasi

Sistem runtime berjalan secara berurutan dalam prioritas: **Environment Variable OS → file konfigurasi lokal `config.json` → Nilai _default_ kode**.

- **Development Mode** (menjalankan server tanpa dicompile): `.env` beserta file `config.json` dibaca secara _relative_ pada CWD project. _Output_ log-nya secara bawaan ditulis di `{project_root}/logs`.
- **Production Mode** (menjalankan versi binary _compiled_): `.env` dan `config.json` diambil dari lokasi publikasi pusat `~/.awlab-id/agent-memory/`. Demikian juga log-nya akan menumpuk di `~/.awlab-id/agent-memory/logs/`.

| Variabel           | Nilai Bawaan   | Deskripsi                                                                                                                                                              |
| ------------------ | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AWLAB_ENV`        | auto           | Memicu status mode _production_ atau _development_ server. (`prod`/`dev`). Secara normal akan terdeteksi otomatis. Saat dibuild via Pyinstaller menjadi mode produksi. |
| `LOG_ENABLED`      | `true`         | Opsi logging _toggle switch_ (`true`/`1`/`yes`, di luar itu akan mematikan log).                                                                                       |
| `LOG_LEVEL`        | `info`         | Ambang batas rincian log (_log verbosity level_): contohnya `info`, `debug`, atau `warning`.                                                                           |
| `DB_PATH`          | (kosong)       | Override absolut ke _custom path_ file SQLite agent-recall jika dikehendaki.                                                                                           |
| `GRAPH_PARALLEL`   | `false`        | Membuka jalan pembuatan code-graph menggunakan multi proses (paralel) / (`true`/`1`/`yes`). (Rincian lebih dalam ada di bawah).                                        |
| `GRAPH_CHUNK_SIZE` | `200`          | Membatasi beban pembuatan _Code Graph_ per _step_ ekstraksi (chunking logic). Memelihara agar beban komputasi CPU dan memori tetap stabil. Diatur `0` untuk skip.      |
| `GRAPH_MAX_FILES`  | (tidak diatur) | Pembatasan keras batas indeks ekstraksi file code pertama.                                                                                                             |

### 🔖 Kapan harus mengaktifkan `GRAPH_PARALLEL`?

Proses pembedahan kode (ekstraksi) saat `graph_build` dipanggil **berjalan urut secara sekuensial secara default**. Bagi hampir seluruh pengguna ini merupakan cara yang paling optimal:

- **Urutan Sekuensial Seringkali Lebih Cepat**
  Waktu ekstraksi dasar AST tiap file cukup singkat. Penguraian _namespace/imports/linking_ justru memakan proses di ujung, dan semua referensi tersebut saling menumpang tumpang-tindih (tethering cross-link) dalam _single-threaded space_. Mengaktifkan multi proses kadang hanya buang-buang waktu _overhead spawn process_, khususnya pada Windows.
- **Dapat menyebabkan Aplikasi (Crash/Hang)**
  Submodul library multi proses (_ProcessPoolExecutor_) dari python seringkali terkunci dan mati bila terperangkap pada aplikasi tipe eksekutor _single one-file_. Jangan menyalakan parameter ini pada aplikasi _production build_.

**Kesimpulan**

Gunakan flag `GRAPH_PARALLEL=1` khusus bila:

1. Code base sistem berjumlah di atas > 5,000 baris ke atas.
2. Sedang merakit dan menjalankan _run instance_ Python di local development via Virtual Environment (`venv`), dan BUKAN eksekusi hasil binary _Pyinstaller build_.

### `GRAPH_CHUNK_SIZE` — strategi ekstraksi graph pada code base besar

Fitur aksi fungsi `graph_build` mampu membungkus dan mengekstraksi kode dalam potongan-potongan terukur (_chunks_). Proses akan memindai deretan batch ukuran (maks `chunk_size` tiap iterasi). File manifes (manifest list file) akan ditandai sukses (checklist) per tahap penyelesaian. Dengan menginisiasi variabel argument opsi `background=true`, komponen _worker background daemon_ MCP akan menyelesaikan file sisa yang menumpuk. Mekanisme ini cocok dan sangat ampuh melindungi _memory spike_ (CPU 100%) dan melahap antrean panjang sisa pemrosesan (queue limit) file di belakang layar saat mengerjakan _repository project_ yang lumayan masif.

---

## 📌 9. Referensi CLI

```bash
python scripts/run.py <command> [options]
```

| Perintah        | Deskripsi                                                                                                           |
| --------------- | ------------------------------------------------------------------------------------------------------------------- |
| `compile-rules` | Mem-parsing file rules/skills `.md` menjadi format spesifik untuk masing-masing agent di direktori `dist/profiles/` |
| `build`         | Melakukan _bundle_ binary executable (bridge dan worker) dan profil agent lalu mengeluarkan ke direktori `dist/`    |
| `publish`       | Mengedarkan (deploy) folder publikasi `dist/` ke lokasi registrasi pusat setiap AI agent                            |
| `test`          | Mengeksekusi suite unit test python (`pytest`)                                                                      |
| `help`          | Dokumentasi bantuan untuk penggunaan berbagai command CLI                                                           |
| `--version`     | Menampilkan identitas string rilis versi _build tag_                                                                |

### 🔖 compile-rules

```bash
python scripts/run.py compile-rules
```

Menguraikan dan mengonversi koleksi aset instruksi AI (`assets/rules/` yang berisi 14 module aturan, serta `assets/skills/` berisi 5 module skill) menjadi dokumen tunggal profil agent spesifik (menurut kompatibilitas prompt-nya).

```
dist/profiles/
├── cline/             # File instruksi terpisah dan sub folder skill (untuk format prompt markdown)
├── copilot/           # File `.instructions.md` ber-metadara (frontmatter YAML) ditambah folder skills
├── claude/            # Satu buah dokumen makro raksasa `CLAUDE.md` beserta skills opsional
├── hermes/            # Terhimpun (packaged) sepenuhnya dalam wujud hierarki subfolder SKILL.md
└── .clinerules        # Salinan instruksi dasar makro project root (hanya output statis)
```

### 🔖 build

```bash
# 📖 Eksekusi build secara utuh penuh (memaketkan profil agent + bundle binary)
python scripts/run.py build

# 📖 Lewati proses pembentukan / bundle executable (build profile agent only)
python scripts/run.py build --no-bin

# 📖 Lewati konversi profil (build bundle executable binary only)
python scripts/run.py build --no-rules
```

### 🔖 publish

```bash
# 📖 Melakukan instalasi global deployment pusat aplikasi (Otomatis mem-build `dist/` bila hilang/kosong)
python scripts/run.py publish

# 📖 Mendistribusikan khusus (cherry-pick) pada satu aplikasi
python scripts/run.py publish --target=claude

# 📖 Mencegah dan melewatkan build bila tidak menginginkan rebuild binary
python scripts/run.py publish --target=all --skip-build

# 📖 Melewatkan segala macam opsi/prompt (skip human intervention)
python scripts/run.py publish --force

# 📖 Hapus atau Un-Install dari daftar profil agen IDE / hapus path sistem lokasi deployment
python scripts/run.py publish --uninstall
```

---

## 📌 10. Pemecahan masalah

| Masalah / Kendala                                                                                  | Solusi Penanganan                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Error/Gagal memicu perintah `pip install -e .`                                                     | Lakukan pemeriksaan ulang versi Python (diwajibkan versi minimum versi 3.10) dan verifikasi ulang jika eksekusi sedang diketik pada jendela terminal CWD root project Anda.                                                                                                                                                                                                                                                          |
| Operasi gagal kompilasi (Build failed) / `dist/bin` terkunci status (_locked_)                     | Ada sebuah proses berjalan (_running instance process_) dari `awlab-ai-worker` yang sedang memegang lock _executable write permission_ di dalam latar belakang. Matikan dahulu proses _worker_ (contoh: eksekusi `scripts/stop-mcp-servers.ps1` untuk Windows PowerShell).                                                                                                                                                           |
| Agent tidak dapat menemukan dan menggunakan fungsi Tool (no MCP Server Available)                  | Anda harus menambah (atau mendaftarkan kembali) parameter konfigurasi _Command entrypoint execution path_ (dari `~/.awlab-id/agent-memory/bin/awlab-ai-assistant`) ke dalam konfigurasi pengaturan AI agent dan melakukan mulai ulang (_restart server IDE/chat app_).                                                                                                                                                               |
| Perintah akses / ekstraksi code _knowledge graph_ terasa sangat memberatkan saat _pertama kali_    | Karena ini adalah inisialisasi awal, algoritma pengekstrak bekerja membuat daftar indeks (full mapping build logic extraction). AI agent dapat membacanya berulang-ulang seusai inisialisasi tersebut selesai (Kondisi status parameter info: `graph_rebuilding: true` mengartikan sesi index-build aktif).                                                                                                                          |
| _Aplikasi ter-Hang/Crash/Macet (un-responsive)_ dengan indikator error terkait multithread paralel | Anda mengaktifkan _opsional command_ sub modul `ProcessPoolExecutor` di parameter variabel. Tolong non-aktifkan (hapus centang opsi / nilai `GRAPH_PARALLEL`) khususnya apabila server sedang menggunakan opsi rilis (executable one file format runtime module).                                                                                                                                                                    |
| Hilangnya sebagian data histori _Memory Bank_ tanpa jejak log                                      | Fitur keamanan offline / fallback _pending changes loop pool logger_ secara reguler selalu merekam status saat database macet / down / bermasalah. AI harus memicu/menjalankan kembali eksekusi Tool command aksi `mem_replay` dengan data muatan _file record tracking offline pending path_ (berasal dari _pending.jsonl_ log history) untuk memunculkan (meng-apply) file perubahan yang tersembunyi/hilang saat kejadian krisis. |

---

## 📌 Langkah berikutnya

- Pelajari segala hal tentang seluruh fitur tool yang disajikan: [Tool MCP yang Tersedia](AVAILABLE_TOOLS.md)
- Kembali menelusuri navigasi [Halaman utama dokumentasi](../../README_ID.md#dokumentasi)

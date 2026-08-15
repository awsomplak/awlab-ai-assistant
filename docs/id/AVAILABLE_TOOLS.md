# Tool MCP yang Tersedia

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumentasi](../../README_ID.md#dokumentasi) · **Tool MCP yang Tersedia**

> MCP server memiliki **2 tool**: `action_call` (dispatcher) dan `action_help` (bantuan), yang menangani **23 action**.

**Di halaman ini:**

- [Arsitektur server](#arsitektur-server)
- [Tool yang tersedia](#tool-mcp-yang-tersedia)
- [Daftar Action](#daftar-action)
- [Pengolahaan User Pattern](#pengolahan-user-pattern)
- [Cache offline (`pending.jsonl`)](#cache-offline-pendingjsonl)
- [Project Family](#project-family)

---

## Arsitektur Server

| Executable | Nama Server | Tool yang Tersedia |
|------------|-------------|--------------------|
| `awlab-ai-assistant.exe` | `awlab-ai-assistant` | `action_call`, `action_help` |

---

## Tool yang Tersedia

### `action_call(action, params=None)`

Tool berikut berfungsi mengirim sebuah action ke MCP Server. Mcp Server akan menjalankan preconditions/pipeline secara otomatis. Setiap respon akan menyertakan `executed`/`skipped`. Contoh penggunaan:

```
action_call(action="task_read", params={"plan_uuid": "mcptool1", "format": "structured"})
```

### `action_help(action=None)`

Tool berikut berfungsi menampilkan informasi bantuan penggunaan untuk setiap action (params, default, contoh, preconditions, pipeline) atau ringkasan per grup jika dipanggil tanpa argumen.

---

## Daftar Action

### context

| Action | Ringkasan |
|--------|-----------|
| `ctx_info` | Membaca konteks project: snapshot, memory-bank, scan, saran, atau konteks orkestrasi. |
| `project_id` | Memeriksa project-id; otomatis membuatnya jika belum ada (idempotent). Panggil ini pada respons pertama, sebelum operasi `mem_*`/plan, agar isolasi memori berjalan optimal dan terisolasi tidak masuk ke DB global. |

### memory

| Action | Ringkasan |
|--------|---------|
| `mem_dedupe` | Menggabungkan entitas memori yang senama (simpan yang berisi data, arsipkan duplikat). |
| `mem_list_entities` | Mendaftar semua entitas memori (nama/tipe/jumlah obs) untuk audit. |
| `mem_observe` | Mencatat pola pengguna ke observation store (`.ai/memory-bank/observations.jsonl`) — input untuk pipeline lanjutan khusus user pattern. |
| `mem_read` | Membaca detail node atau lingkungan graph. |
| `mem_remove` | Mengarsipkan entitas atau menghapus observasi/relasi (type-safe — menolak nama yang ambigu). |
| `mem_replay` | Mengimpor cache offline (`.ai/memory-bank/pending.jsonl`) — menjalankan queue saat store/MCP tidak terjangkau sebelumnya. Entri yang gagal tetap disimpan untuk dicoba ulang. `dry_run` untuk melekukan pratinjau. |
| `mem_search` | Pencarian hybrid BM25+dense di memori (opsional berdasarkan tipe entitas). store=patterns + scope/context untuk pola pengguna yang cakupannya sama ke dalam susunan yang terorganisir, store=family_<slug> untuk memori project yang berkorelasi. |
| `mem_write` | Membuat/menandai entitas, menambah observasi, atau menghubungkan entitas. |

### plan

| Action | Ringkasan |
|--------|---------|
| `plan_status` | Membaca status plan/registry: plan aktif, task berikutnya, status dari task (selesai, pending, belum dieksekusi/dijalankan), fase dari task (fase berisi beberapa task). |
| `plan_update` | Mengubah plan/registry: dipindahkan ke plan aktif, menandai fase selesai, menyelesaikan task yang ditunda. |
| `plan_doc` | Membaca / membuat / memperbarui / menghapus `plan.md` atau `notes.md` suatu plan secara langsung. |
| `reg_update` | Membaca / membuat / memperbarui `registry.md`, berikut detail singkatnya: <br> `create`: plan UUID artifact dibuat oleh mcp → plan baru ditandai sebagai plan yang Aktif ⏹️. <br> `update`: memperbarui status plan menjadi active\|paused\|complete → diletakkan ke tabel yang sesuai dengan status. <br> `delete`: Untuk menghapus plan dan butuh persetujuan ketat pengguna lewat `confirmed=true`. |

### graph

| Action | Ringkasan |
|--------|---------|
| `graph_build` | Membangun/memperbarui `code knowledge graph` ke `.ai/codegraph/` menggunakan graphify. Untuk multi project (`family=<slug>`) mcp akan membangun graph family GABUNGAN (build per-project + tag member:: gabungan) sehingga tetap dapat digunakan meski di lokasi (drive) yang berbeda termasuk ke relasi memori dan akan tergenerate family.html di .ai/codegraph/ tiap project sehingga graph.html tetap tersedia untuk masing-masing project. |
| `graph_status` | Melaporkan status code-graph (tersedia? kedaluwarsa? terdapat perubahan file?). |
| `graph_query` | Mencari di dalam code-graph (label / file source / tipe). Otomatis diperbarui dulu. |
| `graph_path` | Jalur terpendek antara dua node graph. Otomatis diperbarui dulu. |
| `graph_explain` | Menjelaskan node graph (detail + korelasi). Otomatis diperbarui dulu. |

#### Cakupan (scope) yang diindeks

- Graph **hanya AST** dan mengindeks **label tingkat file / fungsi / class / komponen**.
  Variabel lokal/computed/ref/prop **bukan** bagian dari node.
- Saat `graph_query` **tidak menemukan node** untuk suatu istilah, sistem beralih ke fungsi alternatif **pemindaian sumber kata utuh**
  dan mengembalikan hasil kecocokan tingkat file dengan `type: "identifier"` dan `mode: "identifier"` — jadi kueri untuk
  sebuah variabel (mis. `brakeBaselineDays`) tidak pernah menemui jalan buntu.
- **Identitas node terpadu**: semua action graph menerima node `id` (seperti yang dikembalikan
  `graph_query`) maupun label. `graph_path`/`graph_explain` melakukan resolusi dengan urutan id → label → jalur file source →
  nama fungsi → substring, sehingga navigasi lintas-file berfungsi dengan label, id, atau jalur file.
- `graph_path` mencari jalur **tingkat-simbol** terlebih dulu dan bila tidak ditemukan, maka beralih ke jalur **tingkat-modul**
  melalui edge `imports_from`/`imports` (`mode: "module"`), dan jika tetap tidak ditemukan, ia melaporkan
  diagnostik "no path" yang lengkap dengan kedua file source.
- **Import path-alias Vite/JS ikut terindeks** (`@/stores/auth`, `@pages/...`, `~/components/...`).
  Secara default graphify hanya mendukung import relatif + `paths` tsconfig/jsconfig. AWLab-ID **AI-Assisted Development System** menambahkan langkah pasca-build yang membaca `resolve.alias` dari `vite.config.*` / `nuxt.config.*` (dalam bentuk objek atau array, termasuk penggantian `fileURLToPath(new URL(...))`) dan menambahkan edge
  `imports_from`/`imports` yang hilang — sehingga SFC `.vue` dan file yang meng-import `@/` tetap
  terhubung di `graph_path` walau tidak ada `tsconfig.json`. Edge hasil-alias membawa
  `alias_resolved: true`; langkah ini idempotent dan juga memperbaiki sendiri graph lama (tanpa build ulang penuh).

#### Alur pembentukan code-graph

Setiap pembacaan code-graph (`graph_query`, `graph_path`, `graph_explain`) mengembalikan field metadata ini agar agent selalu tahu apakah datanya masih baru dan apakah ada build ulang yang sedang berjalan:

| Field | Tipe | Makna |
|-------|------|---------|
| `graph_fresh` | `bool` | Apakah graph yang disajikan terkini saat dibaca (source tidak berubah sejak build terakhir). |
| `graph_exists` | `bool` | Apakah graph sudah ada (`false` pada pembacaan pertama). |
| `graph_rebuilding` | `bool` | `true` saat build ulang resource yang besar, proses akan berjalan di latar belakang. |
| `graph_built_at` | `str` | Timestamp ISO dari build sukses terakhir. |

**Pola pembaruan data:**
- Graph dengan **sedikit perubahan file** → dibuild ulang secara **sinkronus** sebelum operasi pembacaan (hasilnya akurat).
- Graph dengan **banyak perubahan file (≥ 20) atau saat build pertama** → dibuild ulang di **latar belakang**, karena tidak dapat memberikan hasil secara langsung ketika agent memanggil fungsi ini maka akan ditampilkan data graph sebelumnya jika ada. Jika `graph_rebuilding: true`, agent akan menunggu sejenak lalu membaca kembali graph yang sudah di build ulang (pembacaan berikutnya akan menampilkan data terbaru).
- `graph_build` (eksplisit) yang dipanggil saat build ulang yang sedang berlangsung di latar belakang akan **digabungkan (coalesced)** — operasi ini mengembalikan status `rebuilding: true` alih-alih memulai proses pembangunan ulang yang sama.

### task

| Action | Ringkasan |
|--------|---------|
| `task_read` | Membaca tasks.md dari plan dalam bentuk JSON terstruktur/mentah/minimal. |
| `task_update` | Membuat atau memperbarui tasks.md / task. |

### util

| Action | Ringkasan |
|--------|---------|
| `util_info` | Menampilkan informasi versi mcp server / metadata project (atau pembuatan mermaid diagram). |

### workflow

| Action | Ringkasan |
|--------|---------|
| `wf` | Mendaftar atau menjalankan sebuah workflow yang berada di `~/.awlab-id/agent-memory/work-flows` sesuai dengan nama file workflow nya. |

---

## Pengolahan User Pattern

Mcp server mengolah kebiasaan pengguna yang berulang menjadi opsi yang bisa dipakai ulang:

1. **Observe** — `mem_observe` (yang dijalankan oleh agent saat live chat) atau dari `awlab-ai-assistant.exe hook --agent <host> --event <event>` (event lifecycle host milik agent) menambahkan action ke `.ai/memory-bank/observations.jsonl` untuk melakukan penghapusan duplikat data berdasarkan *fingerprint*.
2. **Bake / Proses Pengolahan** — setiap `action_call` menjalankan `bake_tick` dengan alur kerja `baca → key → hitung → consistency → confidence`. Pattern / pola yang sudah diolah kemudian ditulis ke `.ai/memory-bank/baked.json` hanya jika berubah. Confidence = `frequency(min(1,count/5)) × consistency × source_weight` (`explicit`/`corrected` 0.9, `behavioral` 0.6, `inferred` 0.4). Sebuah pattern / pola butuh `count ≥ 2 ∧ consistency ≥ 0.5 ∧ confidence ≥ 0.6`.
3. **Deliver (tell-once)** — `ctx_info mode="context"` / `mem_search store="patterns"` mengembalikan atau menghasilkan `pattern_candidates` / `baked_patterns` (scoped ke stack). Penanda dalam sistem pengiriman mencatat pola kebiasaan yang telah disampaikan, sehingga pola tersebut TIDAK AKAN PERNAH disampaikan ulang sampai ada pola baru yang mematangkan pola tersebut (*baked*).

**Tiga tahap, satu penyimpanan** — berikut daftar tahapnya:

1. inline (per `action_call`) yang dilakukan oleh agent ketika user melakukan prompt via live chat dengan agent,
2. async (bake-scheduler yang berjalan di latar belakang dan memproses ulang pola / user pattern di workspace yang aktif)
3. sub-agent (`awlab-baker`, yang berjalan jika ada pola baru)
ketiganya menggunakan `observations.jsonl` dan `baked.json` yang sama, sehingga pola yang dihasilkan identik apa pun tahapnya.

**Mode hook (opsional)** — `awlab-ai-assistant.exe hook --agent <host> --event <event>` menangkap observasi dari event lifecycle host (prompt pengguna, penggunaan tool, mulai/selesai-nya sesi, proses dari sub-agent). Registrasi hook wajib dilakukan per-host agent. Executable (mcp yang sudah dibuild contoh dalam bentuk .exe pada windows) menentukan project per event (lihat [`INSTALL.md`](INSTALL.md)).

## Cache offline (`pending.jsonl`)

Saat gagal menyimpan atau server MCP tidak bisa dijangkau, data akan disimpan dalam bentuk **antrian (queue)** — ke `.ai/memory-bank/pending.jsonl`:

- **Sisi server (otomatis):** `mem_write`/`mem_remove` saat store mati, atau `task_update` saat DB-sync mati → operasi diantrekan otomatis.
- **Sisi agent (MCP mati):** ketika agent melakukan `mem_write` / `mem_remove` / `task_update` data akan ditulis ke dalam file JSONL memakai tool file Anda sendiri atau dari IDE atau menulisnya secara manual jika agent memiliki kapabilitas untuk melakukan edit pada perangkat Anda, namun jika dilakukan secara manual tidak menutup kemungkinan data tersebut tidak disimpan atau tidak ditulis oleh agent (sesuai aturan / rules [`14-mcp-offline-cache`](../../assets/rules/14-mcp-offline-cache.md)).
- **Proses Impor ulang:** `mem_replay` mengimpor antrean (queue) data dari cache yang tersimpan offline dari file JSONL (bila ada file atau datanya) — entri yang sukses dijalankan akan dihapus, dan yang gagal akan disimpan kembali untuk dicoba ulang. Fitur `dry_run` akan melakukan pratinjau terlebih dahulu sebelum benar-benar dijalankan atau dieksekusi.

## Project Family

Project gabungan yang berkorelasi meski di lokasi (path atau drive) yang berbeda dan berbagi code-graph gabungan serta penyimpanan memori khusus bernama `family_<slug>`. File `~/.awlab-id/agent-memory/project-families.json` mendaftarkan setiap project kedalam grup dengan bentuk seperti berikut:
```json
{
  "group-atau-family-key": {
    "name": "Nama Group atau Family",
    "members": [
      {
        "path": "/lokasi/path/project/satu",
        "project_id": "id_project_satu"
      },
      {
        "path": "/lokasi/path/project/dua",
        "project_id": "id_project_dua"
      }
    ]
  }
}
```

Jika `project-id` yang terdaftar pada file `project-families.json` berbeda dengan `.ai/project-id` dari project, maka akan lebih diutamakan menggunakan `project-id` dari project tersebut daripada `project-id` yang **dideklarasikan** manual di dalam file `project-families.json` (akan diperbarui otomatis saat build graph family berjalan) karena `project-families` berbasis path dari project sebagai acuan utama. Penambahan project baru ke dalam `project-families.json` akan otomatis diinisialisasi (**seeded**), dan perintah `graph_build` dengan parameter `family=<slug>` akan menghasilkan **code-graph** gabungan yang memuat *node* dengan prefix `<project_id>::`.

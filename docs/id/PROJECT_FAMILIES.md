# 👨‍👩‍👧‍👦 Konfigurasi Project Families (Keluarga Proyek)

> [🏠 BERANDA](../../README_ID.md) · [📚 Dokumentasi](../../README_ID.md#dokumentasi) · **Project Families**

> **Singkatnya (TL;DR):** Untuk berbagi memori dan code-graph di antara project yang saling berkaitan (contoh: backend + frontend), buat file `~/.awlab-id/agent-memory/project-families.json` yang mengelompokkan path mereka. AI akan mendeteksinya secara otomatis.

**Project Family** memungkinkan Anda untuk mengelompokkan beberapa proyek yang saling berkaitan (misalnya, repositori frontend dan repositori backend yang terpisah). Dengan mengonfigurasi _project family_, sistem MCP akan mengaktifkan:

1. **Code Knowledge Graph Gabungan:** Ketika `graph_build` dipanggil dengan parameter `family`, sistem membangun grafik basis kode gabungan lintas proyek, di mana node akan ditandai secara eksplisit dengan `project_id::` masing-masing.
2. **Memori Episodik Bersama:** Agen AI akan memiliki akses ke penyimpanan memori bersama untuk seluruh _family_ (`family_<slug>`), memungkinkan agen untuk menggunakan kembali pola arsitektur, logika bisnis, atau konvensi kode yang telah dipelajari di semua proyek anggota.

## 👤 Siapa yang Menangani Konfigurasi?

> [!IMPORTANT]
> **Project families harus dikonfigurasi secara manual oleh pengembang (Anda).**

Server MCP dan agen AI tidak secara otomatis menebak atau membuat konfigurasi ini karena memutuskan proyek mana yang saling berhubungan adalah keputusan arsitektur tingkat tinggi yang membutuhkan wawasan manusia.

> Setelah Anda mengatur file konfigurasi secara manual, sistem MCP secara otomatis mengambil alih dan menangani semua orkestrasi di balik layar (penggabungan grafik, berbagi memori, rekonsiliasi ID) sepenuhnya secara otonom.

## 🛠️ Praktik Terbaik Konfigurasi

Praktik terbaik untuk mengonfigurasi _project family_ adalah dengan membuat file deklarasi JSON secara manual di direktori konfigurasi AWLab global.

### 🔖 1. 📂 Temukan atau Buat File Konfigurasi

File konfigurasi bernama `project-families.json` dan harus ditempatkan di direktori konfigurasi global Anda:

- **Windows:** `C:\Users\<username>\.awlab-id\agent-memory\project-families.json`
- **Mac/Linux:** `~/.awlab-id/agent-memory/project-families.json`

### 🔖 2. 📝 Tentukan Struktur JSON (format v2)

Gunakan format `v2` yang direkomendasikan untuk memetakan `family_slug` unik ke proyek-proyek anggotanya.

**Contoh `project-families.json`:**

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

**Field:**

- `family_slug` (misalnya, `"my_awesome_product"`): Pengidentifikasi unik untuk keluarga proyek. Gunakan huruf kecil, angka, dan garis bawah (tanpa spasi).
- `name`: Nama yang mudah dibaca manusia untuk keluarga tersebut.
- `members`: Daftar proyek yang terhubung.
  - `path`: Jalur absolut ke direktori proyek di mesin lokal Anda. Gunakan garis miring ke depan (`/`).
  - `project_id`: Nama pendek yang unik untuk proyek (misalnya, `"frontend"`). _Catatan: jika proyek sudah memiliki file `.ai/project-id`, ID lokal tersebut akan secara otomatis dianggap otoritatif dan akan menimpa ID yang dideklarasikan di sini._

### 🔖 3. 🤖 Penggunaan oleh Agen AI

Setelah file ini disimpan, tidak ada konfigurasi lebih lanjut yang diperlukan dari Anda.

Ketika agen AI berinteraksi dengan salah satu proyek ini, agen akan secara dinamis mendeteksi hubungan _family_ tersebut. Agen kemudian dapat menggunakan alat seperti `graph_build` dengan `family="my_awesome_product"` untuk menghasilkan wawasan lintas-repositori, atau menggunakan `mem_search` dengan `store="family_my_awesome_product"` untuk mengingat konvensi arsitektur yang dibagikan.

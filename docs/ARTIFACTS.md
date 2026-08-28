# Artifacts, integrity, and sources

## Downloaded release

- File: `downloads/The_Tournament_Director_Setup_3.7.2.exe`
- Size: 110,285,576 bytes
- SHA-256: `8026b3a6d2a31a783d8cf843c9f2502a78246a8ddc36751246cef1f7577acf59`
- Vendor manifest version: 3.7.2
- Vendor manifest release date: 2020-10-09T16:49:13.432Z
- SHA-512: matches `research/raw/latest.yml`

The installer and inner application executable contain an Authenticode certificate chain whose signer subject is Corey Cooper and whose issuer is DigiCert EV Code Signing CA (SHA2). The certificate containers are under `analysis/signatures/`.

## Extraction inventory

- Installer type: PE32 NSIS self-extracting archive
- Installer entries: 373
- Extracted install tree: about 169 MB
- `app.asar`: 27,431,370 bytes
- `app.asar` SHA-256: `188661a354ccbf0f4bf7c56fdf1d9109498f65b95ca9e3e9d0972964e4266899`
- Inner executable SHA-256: `58f2f814db03ec7ae3d27aa19571dd76c0e947bf0b85423d869ced6b582f16c5`
- Application libraries: 176 JavaScript files; 172 encrypted and 4 plaintext
- Recovered library footprint: about 235,886 lines, including bundled/third-party code
- Shipped display tokens: 156

## Primary sources saved locally

- Official home, download, buy, support, registration, language, FAQ, and gallery pages in `research/raw/`
- Official v3.7 user guide in `research/raw/user-guide-3.7.html`
- Vendor update manifest in `research/raw/latest.yml`
- Shipped `changes.txt`, `userguide.html`, examples, templates, and application resources under `extracted/installer/`
- Official full-size screenshots under `research/screenshots/`

Online sources:

- https://thetournamentdirector.net/
- https://thetournamentdirector.net/download.html
- https://thetournamentdirector.net/assets/userguide/docs370.html
- https://thetournamentdirector.net/buy.html
- https://www.electronjs.org/docs/latest/tutorial/security
- https://www.electronjs.org/docs/latest/tutorial/electron-timelines

## Method

1. Downloaded only from the vendor HTTPS URL.
2. Compared the installer SHA-512 with the vendor's electron-updater manifest.
3. Extracted the NSIS payload with libarchive; did not execute it.
4. Extracted `app.asar` with the standard Electron ASAR tool.
5. Reproduced the shipped bootstrap's AES-CTR source-loading step into a separate analysis directory.
6. Syntax-checked all 176 recovered JavaScript libraries.
7. Cross-checked shipped code/resources against the official guide, release notes, site, and screenshots.

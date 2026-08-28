#!/usr/bin/env node

// Reproduce the application's own release-mode source loading step for
// local static analysis. This does not modify the extracted application.

const fs = require("fs")
const path = require("path")

const projectRoot = path.resolve(__dirname, "..")
const appRoot = path.join(projectRoot, "extracted", "app")
const sourceRoot = path.join(appRoot, "lib")
const outputRoot = path.join(projectRoot, "analysis", "decrypted", "lib")
const aesjs = require(path.join(appRoot, "node_modules", "aes-js"))

function deriveKey() {
  const keySource = fs.readFileSync(path.join(sourceRoot, "tdMainProcWindow.js"), "latin1")
  const keyText = keySource
    .replace(/\s*/g, "")
    .replace(/[\r\n]*/g, "")
    .split("")
    .filter((_character, index) => index % 3 === 0)
    .join("")
    .substring(0, 32)

  return aesjs.utils.utf8.toBytes(keyText)
}

function isEncryptedHex(contents) {
  return contents.length > 0 && contents.length % 2 === 0 && /^[0-9a-f]+$/i.test(contents)
}

function decrypt(contents, key) {
  const encryptedBytes = aesjs.utils.hex.toBytes(contents)
  const counter = new aesjs.ModeOfOperation.ctr(key)
  return aesjs.utils.utf8.fromBytes(counter.decrypt(encryptedBytes))
}

fs.mkdirSync(outputRoot, { recursive: true })

const key = deriveKey()
let decryptedCount = 0
let copiedCount = 0

for (const entry of fs.readdirSync(sourceRoot, { withFileTypes: true })) {
  if (!entry.isFile() || !entry.name.endsWith(".js")) continue

  const inputPath = path.join(sourceRoot, entry.name)
  const outputPath = path.join(outputRoot, entry.name)
  const contents = fs.readFileSync(inputPath, "latin1")

  if (isEncryptedHex(contents)) {
    fs.writeFileSync(outputPath, decrypt(contents, key), "utf8")
    decryptedCount += 1
  } else {
    fs.copyFileSync(inputPath, outputPath)
    copiedCount += 1
  }
}

console.log(`Decrypted ${decryptedCount} source files; copied ${copiedCount} plaintext files.`)
console.log(outputRoot)

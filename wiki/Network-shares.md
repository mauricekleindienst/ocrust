# Network shares

Reading documents off `\\fileserver\scans` is the normal case in an office, and
it is not a local read: SMB and NFS time out, reset connections and answer "the
network name is no longer available" for reasons that have nothing to do with
your file. `ocrust` treats that as the different thing it is.

```bash
ocrust scan '\\fileserver\scans\2026' -f text -o '\\fileserver\ocr'
ocrust scan Z:\scans --workers 4                  # a mapped drive is just a path
ocrust scan /mnt/nfs/incoming                     # NFS or SMB mount, same thing
ocrust scan '\\fileserver\scans' --io-retries 5  # a share that drops often
```

```python
ocr = ocrust.Ocr(io_retries=5)
for doc in ocr.scan_many([r"\\fileserver\scans\2026"]):
    print(doc.source, doc.confidence)
```

## What is handled

| | |
|---|---|
| UNC paths | `\\server\share\...` is an ordinary path everywhere in the library and the CLI |
| Mapped drives | `Z:\scans` likewise |
| POSIX mounts | an NFS or SMB mount is a directory; nothing special is needed |
| Directories | walked recursively, filtered to readable extensions, sorted |
| Patterns | `'\\server\share\*.pdf'` is expanded by `ocrust`, because the Windows shell does not |
| Long paths | the Rust side handles Windows paths past the 260-character limit |

## Retries

Reads inside the engine and writes from the CLI retry **twice** with backoff
(150 ms, then 300 ms) before giving up. What counts as worth retrying:

- **Retried:** connection reset or aborted, broken pipe, timeout, host or
  network unreachable, network down, stale NFS file handle, resource busy,
  unexpected end of file, and the Windows redirector's own codes — network name
  deleted (64), unexpected network error (59), no system resources (1450).
- **Not retried:** "no such file", "permission denied", "invalid data". Those are
  answers; a second attempt returns the same one.

```python
ocrust.Ocr(io_retries=0)     # off, fail on the first hiccup
ocrust.Ocr(io_retries=5)     # a share you do not trust
```

```bash
ocrust scan share/ --io-retries 5
ocrust ocr '\\fileserver\archive\scan.pdf' --io-retries 5
```

## Errors say what actually happened

`Path.exists()` answers *False* both for a missing file and for a server nobody
can reach, which is a bad thing to be told at the start of a batch. `ocrust`
asks again and reports the real reason:

```console
$ ocrust scan '\fileserver\scans6'
ocrust: \fileserver\scans6: [WinError 53] The network path was not found
```

```console
$ ocrust scan /mnt/nfs/incoming
ocrust: /mnt/nfs/incoming: no readable files in this directory
```

## Throughput

On a share the read is usually the slow part, not the OCR, and a page worker
waiting on the network is not using a core. Batches therefore benefit from
workers more than they do locally:

```bash
ocrust scan '\fileserver\scans6' --workers 4     # the default for a batch
```

The numbers on [Performance](Performance.md) were measured on local disk; treat them as the
floor for network work, and measure your own share before tuning. If a batch is
large and the share is slow, copying it locally first is not cheating — it is
usually faster than doing it twice.

## What is not supported

URLs. `http://…/scan.pdf` and `s3://…` are not paths and are not fetched; hand
the library `bytes` instead:

```python
import urllib.request
data = urllib.request.urlopen("https://example.com/scan.pdf").read()
doc = ocr.scan(data, name="scan.pdf")
```

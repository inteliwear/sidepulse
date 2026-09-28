//! Pinned, checksummed downloads into an explicit model cache. Inference itself
//! never contacts the network.
use sha2::{Digest, Sha256};
use std::{
    env, fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};
pub const MODEL_ID: &str = "Qwen/Qwen2.5-0.5B-Instruct-GGUF";
pub const LEGACY_MODEL_ID: &str = "mlx-community/Qwen2.5-0.5B-Instruct-4bit";
pub const MODEL_FILE: &str = "qwen2.5-0.5b-instruct-q4_k_m.gguf";
const MODEL_URL: &str = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf";
const TOKENIZER_URL: &str = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/resolve/7ae557604adf67be50417f59c2c2f167def9a775/tokenizer.json";
const MODEL_HASH: &str = "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db";
const TOKENIZER_HASH: &str = "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539";
pub fn default_cache() -> io::Result<PathBuf> {
    let root = env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache")))
        .or_else(|| env::var_os("LOCALAPPDATA").map(PathBuf::from));
    root.map(|root| root.join("sidepulse/reply/qwen2.5-0.5b-q4-9217f5db"))
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "provide a model cache directory"))
}
fn verified(path: &Path, expected: &str, limit: u64) -> io::Result<bool> {
    let mut file = match fs::File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error),
    };
    if file.metadata()?.len() > limit {
        return Ok(false);
    }
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let size = file.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        hash.update(&buffer[..size]);
    }
    Ok(format!("{:x}", hash.finalize()) == expected)
}
fn download(
    cache: &Path,
    filename: &str,
    url: &str,
    expected: &str,
    limit: u64,
) -> io::Result<PathBuf> {
    let destination = cache.join(filename);
    if verified(&destination, expected, limit)? {
        return Ok(destination);
    }
    if fs::symlink_metadata(&destination).is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "cached model checksum differs; choose a new cache directory",
        ));
    }
    fs::create_dir_all(cache)?;
    let mut file = tempfile::NamedTempFile::new_in(cache)?;
    let response = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(10))
        .timeout_read(Duration::from_secs(60))
        .build()
        .get(url)
        .call()
        .map_err(io::Error::other)?;
    let mut response = response.into_reader().take(limit + 1);
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    let mut total = 0;
    loop {
        let size = response.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        total += size as u64;
        if total > limit {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "model download is too large",
            ));
        }
        hash.update(&buffer[..size]);
        file.write_all(&buffer[..size])?;
    }
    if format!("{:x}", hash.finalize()) != expected {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "model download checksum differs",
        ));
    }
    file.as_file().sync_all()?;
    file.persist_noclobber(&destination)
        .map_err(|error| error.error)?;
    Ok(destination)
}
pub fn download_default(cache: &Path) -> io::Result<(PathBuf, PathBuf)> {
    let tokenizer = download(
        cache,
        "tokenizer.json",
        TOKENIZER_URL,
        TOKENIZER_HASH,
        8000000,
    )?;
    let model = download(cache, MODEL_FILE, MODEL_URL, MODEL_HASH, 600000000)?;
    Ok((model, tokenizer))
}
pub fn cached_default(cache: &Path) -> io::Result<(PathBuf, PathBuf)> {
    let model = cache.join(MODEL_FILE);
    let tokenizer = cache.join("tokenizer.json");
    if !verified(&model, MODEL_HASH, 600000000)? || !verified(&tokenizer, TOKENIZER_HASH, 8000000)?
    {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "download the local model first with --download-model",
        ));
    }
    Ok((model, tokenizer))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    fn model_server(body: &'static [u8]) -> (String, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut input = [0u8; 4096];
            let _ = stream.read(&mut input).unwrap();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            )
            .unwrap();
            stream.write_all(body).unwrap();
        });
        (url, worker)
    }
    #[test]
    fn downloads_publish_only_verified_bytes_and_keep_existing_files() {
        let dir = tempfile::tempdir().unwrap();
        let (url, worker) = model_server(b"model");
        let hash = format!("{:x}", Sha256::digest(b"model"));
        let path = download(dir.path(), "model", &url, &hash, 8).unwrap();
        worker.join().unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"model");
        assert_eq!(download(dir.path(), "model", &url, &hash, 8).unwrap(), path);
        fs::write(&path, b"changed").unwrap();
        assert!(download(dir.path(), "model", &url, &hash, 8).is_err());
        assert_eq!(fs::read(&path).unwrap(), b"changed");
        let (url, worker) = model_server(b"wrong");
        assert!(download(dir.path(), "other", &url, &hash, 8).is_err());
        worker.join().unwrap();
        assert!(!dir.path().join("other").exists());
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 1);
    }
}

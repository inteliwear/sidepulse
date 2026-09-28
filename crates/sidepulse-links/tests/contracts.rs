use serde_json::{Value, json};
use sidepulse_links::*;
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    time::Duration,
};

#[test]
fn legacy_phone_documents_preserve_unknown_fields_and_reject_stale_updates() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("links.json");
    let token = "ab".repeat(32);
    fs::write(
        &path,
        json!({"version":1,"unknown":7,"ios":[{"token":token,"name":"Old","extra":true}]})
            .to_string(),
    )
    .unwrap();
    let mut store = PhoneStore::load(&path).unwrap();
    assert_eq!(store.links()[0].summary().id, "abababababab");
    store
        .register(PhoneLink::new("New", &token, "http://127.0.0.1:7777").unwrap())
        .unwrap();
    let saved: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(saved["unknown"], 7);
    assert_eq!(saved["ios"][0]["extra"], true);
    assert_eq!(saved["ios"][0]["name"], "New");
    assert!(
        !serde_json::to_string(&store.links()[0].summary())
            .unwrap()
            .contains(&token)
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    fs::write(&path, b"{\"version\":1,\"external\":true}").unwrap();
    assert_eq!(
        store.remove("abababababab").unwrap_err().kind(),
        std::io::ErrorKind::AlreadyExists
    );
    assert!(fs::read_to_string(&path).unwrap().contains("external"));
}
#[test]
fn pairing_urls_and_notifications_match_the_phone_protocol() {
    assert_eq!(
        pairing_url("https://bridge.sidepulse.io/", "abcdefghijk", "Desk").unwrap(),
        "sidepulse://p/abcdefghijk"
    );
    assert_eq!(
        pairing_url("http://127.0.0.1:7777", "abcdefghijk", "Desk & 雪").unwrap(),
        "sidepulse://pair?v=1&server=http%3A%2F%2F127.0.0.1%3A7777&channel=abcdefghijk&sender=Desk+%26+%E9%9B%AA"
    );
    assert_eq!(
        notification_payload(
            Some("#00E5FF"),
            Some(" Ready "),
            Some(" Done "),
            "id",
            &json!({"source":{"name":"Desk"}})
        )
        .unwrap(),
        json!({"aps":{"content-available":1,"alert":{"title":"Ready","body":"Done"}},"data":{"source":{"name":"Desk"},"sidepulse_event_id":"id"},"leds":"#00E5FF","title":"Ready","body":"Done"})
    );
    assert_eq!(
        notification_payload(Some("off"), None, None, "id", &json!({})).unwrap(),
        json!({"aps":{"content-available":1},"data":{"sidepulse_event_id":"id"},"leds":"off"})
    );
    let qr = qr_matrix("sidepulse://p/abcdefghijk").unwrap();
    assert!(qr.len() > 21);
    assert!(qr.iter().all(|row| row.len() == qr.len()));
    assert!(qr[0].iter().all(|dark| !dark));
    assert_eq!(normalize_token(&"AB ".repeat(32)).unwrap(), "ab".repeat(32));
    assert!(normalize_token("token").is_err());
}
#[test]
fn actual_http_pairing_and_delivery_use_the_registered_phone() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let server = format!("http://{}", listener.local_addr().unwrap());
    let token = "cd".repeat(32);
    let expected_token = token.clone();
    let mock = std::thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut reader = BufReader::new(stream);
        let mut first = String::new();
        reader.read_line(&mut first).unwrap();
        assert!(first.starts_with("GET /api/leds/abcdefghijk "));
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if line == "\r\n" {
                break;
            }
        }
        let body = format!(
            "data: {{\"v\":1,\"type\":\"wrong\"}}\n\ndata: {}\n\n",
            json!({"v":1,"type":"ios_registration","device":{"bundle_id":"io.sidepulse.ios","name":"Test phone","push_token":expected_token}})
        );
        write!(reader.get_mut(),"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
        drop(reader);
        let (stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut reader = BufReader::new(stream);
        let mut first = String::new();
        reader.read_line(&mut first).unwrap();
        assert!(first.starts_with(&format!("POST /api/leds/apns_{expected_token} ")));
        let mut length = 0;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if line == "\r\n" {
                break;
            }
            if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                length = value.trim().parse::<usize>().unwrap();
            }
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        let body: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(body["leds"], "off");
        assert_eq!(body["data"]["sidepulse_event_id"], "test-event");
        reader
            .get_mut()
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            .unwrap();
    });
    let link = receive_registration_once(&server, "abcdefghijk", Duration::from_secs(3), || true)
        .unwrap()
        .unwrap();
    assert_eq!(link.token, token);
    assert_eq!(link.name, "Test phone");
    assert_eq!(
        send_program(&link, Some("off"), None, None, "test-event", &json!({})).unwrap(),
        "ok"
    );
    mock.join().unwrap();
    assert!(
        receive_registration_once(&server, "abcdefghijk", Duration::from_secs(3), || false)
            .unwrap()
            .is_none()
    );
}

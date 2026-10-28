# mocopi UDP Receiver (Docker)

mocopi アプリからの UDP パケットを受信し、受信レート、送信元、パケットサイズ、先頭 32 バイトを表示します。受信データはバイナリのままで、モーション情報へのデコードは行いません。

## 起動

Docker Desktop を起動し、このフォルダで実行します。

```sh
docker compose up --build
```

停止するには `Ctrl+C` を押してから `docker compose down` を実行します。

## mocopi アプリの設定

1. PC とスマートフォンを同じローカルネットワークに接続します。
2. PC の IPv4 アドレスを確認します（Windows では `ipconfig`）。
3. mocopi アプリの PC 接続設定で、その PC の IPv4 アドレス、UDP ポート `12351`、送信フォーマット `mocopi (UDP)` を指定します。
4. モーション画面で送信を開始し、このターミナルに受信ログが出ることを確認します。

`localhost` やコンテナの IP アドレスではなく、Windows PC の LAN 側 IPv4 アドレスをアプリに設定してください。Windows ファイアウォールで UDP `12351` の受信が許可されている必要があります。

## ポート変更

`compose.yaml` のホスト側・コンテナ側ポートと `MOCOPI_UDP_PORT` を同じ番号に変更してください。

## 注意

mocopi の UDP モーションデータは暗号化されません。ローカルネットワーク内で使用してください。

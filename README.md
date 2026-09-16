# SNS Media Collector v0.3.4

X / Twitter と pixiv の画像・動画を、既存のHitomi Downloaderフォルダを引き継ぎながら収集するWindowsアプリです。

## 今回の更新

- Xとpixivのアプリ内ログインを、アカウントごとに完全分離しました。
- 新しいXアカウントは「アプリ内Xログイン」が最初から選択されます。
- XのCookie保存後・取り込み後に認証テストを自動実行します。
- XからログアウトしたCookieを古い認証として再保存しないようにしました。
- アカウント一覧へ `✓`（認証保存済み）/ `⚠`（ログイン必要）を表示します。
- 左側からXのログインだけをすぐ更新できます。
- pixivの旧手動OAuthを置き換えました。F12、Networkタブ、30秒以内のcodeコピー、refresh token手入力は不要です。
- pixivのOAuth callbackをアプリが受け取り、refresh tokenをアカウント別gallery-dlキャッシュへ保存します。
- X Cookie値とpixiv refresh tokenは画面・アプリログ・`accounts.json`・Gitリポジトリへ出しません。
- 従来のcookies.txt、ブラウザCookie、pixiv refresh token手入力も互換用に残しています。

## インストールと更新

初回は[最新Release](https://github.com/kosh08/sns-media-collector/releases/latest)の
`SNSMediaCollector-Setup-<version>.exe`を実行してください。

インストール後はスタートメニューまたはデスクトップから起動できます。以後はアプリ右下の
「更新を確認」で新版を取得できます。設定や認証、Likesアンカー、archive、カタログは
`%USERPROFILE%\SNSMediaCollector`（変更済みなら指定したデータフォルダ）に残るため、ZIPの再展開や再設定は不要です。

## Xを設定する

1. 左側の「＋ 追加」を押します。
2. 表示名を入力します。認証方式は「アプリ内Xログイン（アカウント別・推奨）」が選択済みです。
3. 「Xへログイン / 更新」で使用するXアカウントへログインします。
4. 「このログインを保存」を押します。続けて認証テストが実行されます。
5. アカウント設定を保存します。

次回からログイン設定はそのまま使えます。Cookieが失効したときだけ、左側の
「Xログイン / Cookie更新」を押してください。ChromeやBraveの既定ブラウザ、そちらのログイン状態には依存しません。

アプリ内ログインがX側に拒否される環境では、Netscape形式のcookies.txtをアカウント編集画面から取り込めます。
CookieそのものをGitHubやチャットへアップロードしないでください。

## pixivを設定する

1. 左側の「＋ 追加」でサービスをpixivへ変更します。
2. 表示名を入力します。認証方式は「アプリ内pixiv連携（アカウント別・推奨）」へ自動で切り替わります。
3. 「pixivへログイン / 更新」でログインします。
4. callbackとrefresh tokenの保存、認証テストはアプリが自動処理します。
5. アカウント設定を保存します。

pixivの連携情報はアカウント専用SQLiteキャッシュに保存されます。複数アカウントを登録しても相互に混ざりません。

## X Likes差分取得

Hitomi Downloaderから移行する場合は、保存先を選んで「Hitomi保存先を引き継ぐ」を先に実行します。
その後、X / いいねを選び「Likesアンカーを作成（DLなし）」で現在位置を保存してください。

以後の「新しいいいねだけ」は次の順で動作します。

1. 最大指定件数までLikesを軽く確認します（画像DLなし）。
2. 保存済みアンカーに到達したら、その手前だけを新規候補にします。
3. gallery-dl archiveと既存Hitomiファイルを照合します。
4. 未取得メディアだけを保存します。
5. 実ファイルの存在・サイズ・保存先を確認できた場合だけアンカーを進めます。

アンカーが探索上限内で見つからない場合や保存確認に失敗した場合は、安全のため境界を進めません。

## データと認証

- アプリ本体: `%LOCALAPPDATA%\Programs\SNSMediaCollector\versions\<version>`
- ユーザーデータ: `%USERPROFILE%\SNSMediaCollector`
- Xアカウント別Cookie: `auth\cookies\x-<profile-id>.txt`
- X/pixivログイン領域: `auth\web-profiles\<service>-<profile-id>`
- pixivアカウント別OAuthキャッシュ: `auth\pixiv\pixiv-<profile-id>.sqlite3`

アンインストールとアプリ更新はユーザーデータを削除しません。

## 開発と検証

Python 3.10以上でソース版を起動できます。

```powershell
setup_and_run.cmd
```

全テストは次で実行します。

```powershell
full_test.cmd
```

`main`へのpushで`.github/workflows/windows-installer.yml`が以下を検証します。

1. Python 3.10構文互換、単体テスト、Qt回帰テスト、起動スモークテスト
2. PyInstallerによるGUI・gallery-dl・更新ヘルパーの生成
3. QtWebEngineとX/pixiv extractorを含む梱包後自己検査
4. Inno Setupインストーラー生成
5. 使い捨てWindows環境で旧版→新版更新、再インストール、起動、アンインストール
6. ユーザーデータ保持とSHA-256検証
7. 全工程成功後だけGitHub Releaseを公開

実アカウントへのログインと実サイトからの取得だけは、配布後に各ユーザーのPCで確認します。テストへCookieやtokenは使用しません。

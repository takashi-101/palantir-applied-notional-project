# 中古車リース 車両と不具合の管理（Palantir Learn「Applied Notional Project」の成果物）

![tests](https://github.com/takashi-101/palantir-applied-notional-project/actions/workflows/tests.yml/badge.svg) ![Python](https://img.shields.io/badge/Python-3.11%2B-blue) ![依存](https://img.shields.io/badge/依存-標準ライブラリのみ-green) ![License](https://img.shields.io/badge/license-MIT-lightgrey)

公開URL：https://takashi-101.github.io/palantir-applied-notional-project/

## 概要（30秒で読めます）

- **課題**：Palantir Learn の実践課題「Applied Notional Project」。中古車リース会社の担当マネージャーが、保有車両を見渡し、1台ずつ確かめ、不具合を解決するためのアプリを作る。目的は「データの掃除」「車両一覧」「不具合の受付箱」の3つ。
- **作ったもの（画面）**：同じ3つの目的を、ブラウザだけで動く1枚のページにしました。生データを7つの規則で直す工程、絞り込みと詳細表示のある車両一覧、重要度と経過日数で並ぶ不具合の受付箱を備えています。
- **作ったもの（エンジン）**：画面の裏側にあたるオントロジー（オブジェクト型・リンク型・アクション型）を、Python の標準ライブラリと SQLite だけで動かす `ontology_engine.py` です。状態を書き換えられるのは定義済みのアクションだけで、引数・権限・提出条件を確かめてから反映し、却下も含めて監査ログに残します。AIエージェントは提案しかできず、反映には人の承認が要ります。
- **Foundryとの対応**：画面の各部分が、Foundry では Pipeline Builder・Ontology Manager・Workshop・Action のどれで作るものにあたるかを、ページの最後に表で示しています。
- **学びの裏付け**：成果物の部分ごとに、裏付けになる学習と照会先を、下の「根拠（学習）」の表に並べています。

車両・不具合・担当者のデータはすべて架空です。画面（index.html）での操作の記録は、開いた端末のブラウザにだけ保存されます。エンジンは外部への通信を行いません。

## このリポジトリにあるもの

| ファイル | 内容 |
|---|---|
| `index.html` | ブラウザで動く画面（データの掃除、車両一覧、不具合の受付箱） |
| `ontology_engine.py` | オントロジーのエンジン（1ファイル。外部パッケージなし） |
| `tests/test_ontology_engine.py` | エンジンのテスト（35件） |
| `.github/workflows/tests.yml` | push のたびに、Python 3.11・3.12・3.13 でテストと実行例を動かす設定 |

## 課題と設計

**誰が何に困るか**：生成AIやエージェントに業務データの更新を任せると、次の3つが起きます。

1. 誤った更新が、確かめる人のいないまま入る
2. 誰が、なぜ変えたのかを、後から追えない
3. 見せなくてよい項目（個人情報など）まで、AIに渡る

**どう解いたか**：業務の決まりごとを「オブジェクト型」「リンク型」「アクション型」に分け、書き換えの入口をアクションだけにしました。

| 設計 | 実装 |
|---|---|
| 書き換えの入口は4つだけ | `submit`（人が実行）／`propose`（提案）／`approve`（承認）／`decline`（見送り） |
| アクションごとに、引数・実行できる役割・提出条件を定義する | `ACTIONS`、`_gate`、`_rule_*` |
| AIエージェントは提案だけ。人が承認して反映する。承認した時点の状態で条件を確かめ直す。自分の提案は自分で承認できない | `propose`、`approve` |
| 影響の大きい操作は、理由の入力を求める | `needs_reason`（未解決に戻す、重要度の引き上げ） |
| 個人情報の印を付けた項目は、見る資格のない役割に伏せる | `Prop.marking`、`read` |
| 反映も却下も、追記だけの監査ログに残す。記録ごとに前の記録のハッシュを含め、書き換えに気づけるようにする | `audit_log`、トリガー、`verify` |
| アクションを通さずに状態が変わったら、検証で気づける | 記録ごとに全体の状態のハッシュを残す（`_state_hash`） |
| アクション型を、LLM に渡す「道具の定義」として出力する | `tool_definitions`、`--tools` |

### 型の一覧

| 種類 | 名前 | 内容 |
|---|---|---|
| オブジェクト型 | `Vehicle`（車両） | メーカー、車種、年式、走行距離、状態（貸出中／待機／整備中） |
| オブジェクト型 | `Issue`（不具合） | 内容、重要度（高／中／低）、受付日、状態（未解決／解決済）、対応の記録 |
| オブジェクト型 | `Staff`（担当者） | 氏名（個人情報の印つき）、所属、役割、同時に担当できる件数 |
| リンク型 | `issue_vehicle` | 不具合 → 車両（多対1。書き換え不可） |
| リンク型 | `issue_assignee` | 不具合 → 担当者（多対1。アクションで付け替える） |

| アクション型 | 内容 | 実行・承認できる役割 | 理由 | 提出条件 |
|---|---|---|---|---|
| `assign_issue` | 不具合に担当者を割り当てる | マネージャー、整備担当 | 任意 | 未解決であること。担当は整備担当に限る。担当中の未解決が上限に達していないこと |
| `raise_severity` | 重要度を引き上げる | マネージャー、整備担当 | 必須 | 未解決であること。今より高い重要度であること |
| `resolve_issue` | 解決済にする（対応の記録つき） | マネージャー、整備担当 | 任意 | 未解決で、担当者が決まっていること。整備担当は自分の担当分だけ |
| `reopen_issue` | 未解決に戻す | マネージャー | 必須 | 解決済であること |
| `set_vehicle_status` | 車両の状態を変える | マネージャー | 任意 | 今と違う状態であること。未解決で重要度「高」の不具合がある車両は、貸出中にできない |

## 動作の流れ

```text
  人（マネージャー・整備担当）            AIエージェント
        |                                     |
        | submit                              | propose（理由が必須）
        |                                     v
        |                          提案（承認待ち。まだ反映しない）
        |                                     |
        |                                     | 人が approve ／ decline
        v                                     v
  +---------------------------------------------------------------+
  | 1. 引数の確認   定義にない引数・不足・選択肢の外・存在しないID   |
  | 2. 権限の確認   役割ごとの可否。AIエージェントの直接実行は不可   |
  | 3. 提出条件     今の状態とリンクを読んで判定（承認時にもう一度） |
  +---------------------------------------------------------------+
        |  通った                             |  通らない
        v                                     v
  オブジェクトとリンクを更新              何も変えない
        |                                     |
        +------------------+------------------+
                           v
     監査ログに追記（反映・却下・提案・見送り。前の記録のハッシュを含む）
```

## 使い方

Python 3.11 以上で動きます。外部パッケージは使いません。

```bash
python3 ontology_engine.py                 # 実行例（メモリ上で動き、ファイルを残しません）
python3 ontology_engine.py --tools         # LLM に渡す道具の定義（JSON）を表示
python3 ontology_engine.py --db fleet.db   # SQLite のファイルに残す（sqlite3 で中身を確かめられます）
python3 -m unittest discover -s tests -v   # テスト
```

## 実行結果

`python3 ontology_engine.py` の出力です。実行例は決まった時計（2026-10-24 09:00 から5分刻み）で動くので、だれが実行しても同じ出力、同じハッシュになります。このブロックと実際の出力が一致することを、テストで確かめています。

<!-- demo:start -->
```text
■ 定義
  オブジェクト型：Vehicle（車両）18件／Issue（不具合）13件／Staff（担当者）3件
  リンク型　　　：issue_vehicle（不具合→車両）13本／issue_assignee（不具合→担当者）0本
  アクション型　：assign_issue／raise_severity／resolve_issue／reopen_issue／set_vehicle_status

■ 操作（状態の書き換えは、すべてアクションを通る）
 1. [反映] S-02 整備担当：I-104（ブレーキの効きが弱い・高）を自分に割り当てる
      変更：Issue:I-104 の issue_assignee　なし → S-02
 2. [反映] S-02 整備担当：I-105（オイル漏れ・高）を自分に割り当てる
      変更：Issue:I-105 の issue_assignee　なし → S-02
 3. [却下] S-02 整備担当：I-109（バッテリー上がり・高）も自分に割り当てる
      理由：S-02 は担当中の未解決が2件で、上限（2件）に達している
 4. [却下] S-01 マネージャー：C-008（デミオ・整備中）を貸出中にする
      理由：未解決で重要度「高」の不具合（I-105）がある車両は貸出中にできない
 5. [反映] S-02 整備担当：I-105 を解決済にする
      変更：Issue:I-105 の status　未解決 → 解決済
      変更：Issue:I-105 の memo　なし → オイルパンのガスケットを交換。漏れが無いことを確認
 6. [反映] S-01 マネージャー：C-008 を待機にする
      変更：Vehicle:C-008 の status　整備中 → 待機
 7. [却下] AI-01 AIエージェント：I-109 を解決済にしようとする
      理由：AIエージェントは直接実行できない（提案して、人の承認を受ける）
 8. [提案] AI-01 AIエージェント：I-109 を S-03 に割り当てる案を出す
      提案番号 1（人の承認待ち。まだ反映していない）
 9. [反映] S-01 マネージャー：提案番号1を承認する
      変更：Issue:I-109 の issue_assignee　なし → S-03
10. [却下] S-03 整備担当：I-113（解決済）を未解決に戻す
      理由：整備担当はこのアクションを実行できない
11. [却下] S-01 マネージャー：I-113 を、理由を書かずに未解決に戻す
      理由：このアクションは理由の入力が必要
12. [反映] S-01 マネージャー：I-113 を、理由を書いて未解決に戻す
      変更：Issue:I-113 の status　解決済 → 未解決

■ 読み取り（氏名は個人情報の印つき。見る資格のない役割には伏せる）
  S-01 マネージャーが見た S-02：見本 花子
  AI-01 AIエージェントが見た S-02：＊＊＊

■ 監査ログ（13件。却下も残る）
   1  09:00  開始   system -                   OK                4f64ef4c
   2  09:05  反映   S-02   assign_issue        OK                14c41b7e
   3  09:10  反映   S-02   assign_issue        OK                5e855a5b
   4  09:15  却下   S-02   assign_issue        OVER_CAPACITY     5b2d5774
   5  09:20  却下   S-01   set_vehicle_status  BLOCKED_BY_ISSUE  f8219720
   6  09:25  反映   S-02   resolve_issue       OK                e7493e5f
   7  09:30  反映   S-01   set_vehicle_status  OK                523448d7
   8  09:35  却下   AI-01  resolve_issue       AI_MUST_PROPOSE   bcdac4fd
   9  09:40  提案   AI-01  assign_issue        OK                8be48bef
  10  09:45  反映   S-01   assign_issue        OK                d8506f22（提案 AI-01）
  11  09:50  却下   S-03   reopen_issue        FORBIDDEN         28ac1dc3
  12  09:55  却下   S-01   reopen_issue        REASON_REQUIRED   a195a16e
  13  10:00  反映   S-01   reopen_issue        OK                6c1eb961

■ 検証
  連鎖と現在の状態：問題なし
  監査ログを書き換えようとする：拒否された（audit_log は追記のみ（書き換え不可））
  アクションを通さず I-101 を解決済に書き換える：現在の状態が、最後の記録と一致しない（アクションを通さない変更がある）
  書き換えを取り消したあと：問題なし
```
<!-- demo:end -->

## テスト（35件）

- 初期データ：件数、リンク先の実在、型と選択肢
- 引数：定義にないアクション、登録のない実行者、不足と定義外、型、選択肢、存在しないID、文字数、SQLの断片を含む値
- 提出条件：担当件数の上限、担当にできる役割、解決済にできる人、重要度は引き上げのみ、貸出中にできない車両、未解決に戻す権限と理由、却下では何も変わらないこと
- 提案と承認：AIエージェントの直接実行の却下、理由の必須、承認で反映され提案者と承認者の両方が残ること、AIエージェントは承認できないこと、自分の提案は承認できないこと、承認者の役割、承認時の再確認、見送り
- 読み取り：個人情報の印を付けた項目の伏せ字
- 監査ログ：却下の記録、形の崩れた入力の記録、追記のみ（書き換え・削除の拒否）、トリガーを外して書き換えた場合の検出、途中の記録の削除の検出、アクションを通さない変更の検出
- 道具の定義と実行例：全アクションの定義、実行例の再現性、README の実行結果との一致、ファイルへの保存

## 根拠（学習）

成果物の部分ごとに、裏付けになる学習と照会先を並べました。Palantir Learn の修了証（Badge of Completion）は、照会先を開くと第三者が修了を確かめられます。

| 機能 | 裏付けになる学習（発行元・名称・発行日） | 照会先 |
|---|---|---|
| 生データを7つの規則で直す（`index.html` 目的1） | Palantir Technologies「Deep Dive: Building Your First Pipeline」2026年10月24日 | https://verify.skilljar.com/c/eshgfevgsdpc |
| 車両一覧と不具合の受付箱の画面（`index.html` 目的2・目的3） | Palantir Technologies「Deep Dive: Building Your First Application」2026年10月24日 | https://verify.skilljar.com/c/mgad4nzqmcsa |
| 車両一覧の絞り込みと詳細表示 | Amazon Web Services Training and Certification「AWS Knowledge: Business Intelligence - Training Badge」2025年11月10日 | https://www.credly.com/badges/47e6d276-9782-4f77-ba7a-f60e486e7ed2 |
| オブジェクト型・リンク型・アクション型の定義（`OBJECT_TYPES`、`LINK_TYPES`、`ACTIONS`） | Palantir Technologies「Deep Dive: Creating Your First Ontology」2026年10月24日 | https://verify.skilljar.com/c/6q7juir22ytx |
| 複数のオブジェクトとリンクを読んで判定する規則（`_rule_assign`、`_rule_vehicle_status` ほか） | Palantir Technologies「Speedrun: Your First Ontology Function」2026年10月24日 | https://verify.skilljar.com/c/995cz4xr3rcb |
| アクション型を、AIに渡す道具の定義にする（`tool_definitions`） | Palantir Technologies「Speedrun: Your First AIP Workflow」2026年10月24日 | https://verify.skilljar.com/c/mn24dnddn7sk |
| AIエージェントの提案を、アクションの形と理由つきで受け、人が承認して反映する（`propose`、`approve`、`decline`） | Palantir Technologies「Speedrun: Your First Agentic AIP Workflow」2026年10月24日 | https://verify.skilljar.com/c/wpfcqyvznoie |
| 理由の入力を求める操作、個人情報の印と伏せ字、操作の記録（`needs_reason`、`Prop.marking`、`read`、`audit_log`） | Palantir Technologies「Deep Dive: Data Protection Tools in Foundry」2026年10月24日 | https://verify.skilljar.com/c/ftf84akuvcjb |
| Foundry と AIP の用語での整理（`index.html` の「Foundryとの対応」） | Palantir Technologies「Foundry & AIP Builder Foundations」2026年10月24日 | https://verify.skilljar.com/c/uri2mxcyfmts |

Palantir Learn の修了証は全部で18件あり、どれも証明書番号で https://verify.skilljar.com/c/【番号】 から照会できます。

## 検出できる範囲と、できない範囲

- 検証（`verify`）で分かるのは、監査ログの書き換え、途中の記録の削除、アクションを通さない状態の変更です。SQLite のファイルを直接開ける人がトリガーを外し、末尾の記録のうち状態を変えないもの（却下・提案）だけを消した場合は、分かりません。
- LLM は呼びません。実行例のAIエージェントの提案は、LLM の出力にあたる内容を手で書いた見本です。`--tools` の定義を LLM に渡せば、同じ入口（`propose`）につなげられます。
- Foundry の機能を再現したものではありません。オントロジーの考え方を、小さなコードで確かめるためのものです。

## ライセンス

MIT

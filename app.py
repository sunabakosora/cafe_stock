from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
import os
from functools import wraps

app = Flask(__name__)
app.secret_key = "cafe_secret_key_123"   # セッション用（適当でOK）

# DBパス
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cafe_stock.db")

# =====================
# DB接続
# =====================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# =====================
# ログイン必須 decorator
# =====================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


# =====================
# ログイン画面
# =====================
from werkzeug.security import check_password_hash, generate_password_hash

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form["name"]
        password = request.form["password"]

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE name = ?",
            (name,)
        ).fetchone()
        conn.close()

        if user:
            # パスワードチェック（平文の場合）
            if password == user["password"]:
                # セッションにユーザー情報と role を保存
                session["user_id"] = user["id"]
                session["user_name"] = user["name"]
                # role が空なら staff にする（安全対策）
                session["role"] = user["role"] if user["role"] else "staff"
                return redirect(url_for("dashboard"))
            else:
                return "パスワードが間違っています"
        else:
            return "ユーザーが存在しません"

    return render_template("login.html")



# =====================
# ログアウト
# =====================
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =====================
# ダッシュボード（トップ画面）
# =====================
@app.route("/")
@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db_connection()

    # 在庫一覧
    items = conn.execute("""
        SELECT 
            items.id,
            items.name,
            items.unit,
            items.min_stock,
            IFNULL(ic.quantity, 0) AS stock
        FROM items
        LEFT JOIN inventory_current ic ON items.id = ic.item_id
        ORDER BY items.name
    """).fetchall()

    # 不足在庫
    low_items = conn.execute("""
        SELECT 
            items.id,
            items.name,
            (items.min_stock - IFNULL(ic.quantity, 0)) AS shortage,
            items.unit,
            u.name AS buyer_name,
            pp.status AS purchase_status
        FROM items
        LEFT JOIN inventory_current ic ON items.id = ic.item_id
        LEFT JOIN purchase_plan pp ON items.id = pp.item_id AND pp.status != '完了'
        LEFT JOIN users u ON pp.user_id = u.id
        WHERE IFNULL(ic.quantity, 0) < items.min_stock
        ORDER BY shortage DESC
    """).fetchall()


    # ここで仕入れ先を取得
    suppliers = conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()

    conn.close()
    return render_template(
        "dashboard.html",
        items=items,
        low_items=low_items,
        suppliers=suppliers  # ← テンプレートに渡す
    )



# =====================
# 商品一覧（管理用）
# =====================
@app.route("/items")
@login_required
def items_list():
    # 店主でなければアクセス禁止
    if session.get("role") != "owner":
        return "権限がありません", 403

    conn = get_db_connection()
    items = conn.execute("""
        SELECT 
            items.id,
            items.name,
            items.unit,
            items.min_stock,
            IFNULL(ic.quantity, 0) AS stock
        FROM items
        LEFT JOIN inventory_current ic ON items.id = ic.item_id
        WHERE items.is_deleted = 0
        ORDER BY items.id ASC
    """).fetchall()
    conn.close()
    return render_template("items_list.html", items=items)


# =====================
# 在庫履歴一覧
# =====================
# 在庫履歴一覧
@app.route("/inventory/logs")
@login_required
def inventory_logs():
    conn = get_db_connection()

    logs = conn.execute("""
        SELECT 
            il.id,
            il.change_type,
            il.quantity,
            il.note,
            il.created_at,
            items.name AS item_name,
            s.name AS supplier_name,
            u.name AS user_name
        FROM inventory_logs il
        LEFT JOIN items ON il.item_id = items.id
        LEFT JOIN suppliers s ON il.supplier_id = s.id
        LEFT JOIN users u ON il.user_id = u.id
        ORDER BY il.created_at DESC
    """).fetchall()

    conn.close()
    return render_template("inventory_logs.html", logs=logs)


# =====================
# 商品登録
# =====================
@app.route("/items/add", methods=["GET", "POST"])
@login_required
def item_add():
    conn = get_db_connection()

    if request.method == "POST":
        name = request.form["name"]
        unit = request.form["unit"]
        min_stock = request.form.get("min_stock") or 0

        conn.execute("""
            INSERT INTO items (name, unit, min_stock)
            VALUES (?, ?, ?)
        """, (name, unit, min_stock))

        conn.commit()
        conn.close()
        return redirect(url_for("items_list"))

    conn.close()
    return render_template("item_add.html")

# =====================
# 入庫処理（仕入れ先記録対応）
# =====================
@app.route("/stock_in/<int:item_id>", methods=["POST"])
@login_required
def stock_in(item_id):
    qty = int(request.form["qty"])
    supplier_id = request.form.get("supplier_id")  # モーダルから送信される値

    conn = get_db_connection()

    # 在庫テーブル更新
    conn.execute("""
        INSERT INTO inventory_current (item_id, quantity)
        VALUES (?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            quantity = quantity + ?,
            updated_at = CURRENT_TIMESTAMP
    """, (item_id, qty, qty))

    # 在庫ログ登録（supplier_id 追加）
    conn.execute("""
        INSERT INTO inventory_logs (item_id, change_type, quantity, user_id, note, supplier_id)
        VALUES (?, 'IN', ?, ?, '入庫', ?)
    """, (item_id, qty, session["user_id"], supplier_id))

    conn.commit()
    conn.close()

    return redirect(url_for("dashboard"))


# =====================
# 出庫処理
# =====================
@app.route("/stock_out/<int:item_id>", methods=["POST"])
@login_required
def stock_out(item_id):
    qty = int(request.form["qty"])
    out_type = request.form.get("out_type")  # '出庫' or '廃棄'

    conn = get_db_connection()

    # 在庫テーブルの更新
    conn.execute("""
        UPDATE inventory_current
        SET quantity = quantity - ?, updated_at = CURRENT_TIMESTAMP
        WHERE item_id = ?
    """, (qty, item_id))

    # 在庫ログ登録
    conn.execute("""
        INSERT INTO inventory_logs (item_id, change_type, quantity, user_id, note)
        VALUES (?, 'OUT', ?, ?, ?)
    """, (item_id, qty, session["user_id"], out_type))

    conn.commit()
    conn.close()

    return redirect(url_for("dashboard"))



# =====================
# 削除処理
# =====================
@app.route("/items/delete/<int:item_id>", methods=["POST"])
@login_required
def item_delete(item_id):
    conn = get_db_connection()
    conn.execute("""
        UPDATE items
        SET is_deleted = 1
        WHERE id = ?
    """, (item_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("items_list"))

# =====================
# 購入予定登録
# =====================
@app.route("/purchase_plan/add/<int:item_id>", methods=["POST"])
@login_required
def purchase_plan_add(item_id):
    user_id = request.form.get("user_id")  # 選択された担当者ID
    qty = request.form.get("qty") or 1     # 必要に応じて数量も入力可

    conn = get_db_connection()
    conn.execute("""
        INSERT INTO purchase_plan (item_id, user_id, qty, status)
        VALUES (?, ?, ?, '未完了')
    """, (item_id, user_id, qty))
    conn.commit()
    conn.close()

    return redirect(url_for("dashboard"))

# =====================
# 👩‍🍳 ユーザー管理（店主専用）
# =====================

@app.route("/users")
@login_required
def users_list():
    # 店主のみ閲覧可能
    if session.get("role") != "owner":
        return "権限がありません", 403

    conn = get_db_connection()
    users = conn.execute("SELECT id, name, role FROM users").fetchall()
    conn.close()
    return render_template("users.html", users=users)


@app.route("/users/add", methods=["GET", "POST"])
@login_required
def users_add():
    # 店主のみ登録可能
    if session.get("role") != "owner":
        return "権限がありません", 403

    if request.method == "POST":
        name = request.form["name"]
        password = generate_password_hash(request.form["password"])
        role = request.form["role"]

        conn = get_db_connection()
        conn.execute("""
            INSERT INTO users (name, password, role)
            VALUES (?, ?, ?)
        """, (name, password, role))
        conn.commit()
        conn.close()

        return redirect(url_for("users_list"))

    return render_template("user_add.html")

# =====================
# 仕入れ先一覧
# =====================
@app.route("/suppliers")
@login_required
def suppliers_list():
    conn = get_db_connection()
    suppliers = conn.execute("SELECT * FROM suppliers ORDER BY id").fetchall()
    conn.close()
    return render_template("suppliers.html", suppliers=suppliers)


# =====================
# 仕入れ先追加
# =====================
@app.route("/suppliers/add", methods=["GET", "POST"])
@login_required
def suppliers_add():
    if request.method == "POST":
        name = request.form["name"]
        note = request.form["note"]

        conn = get_db_connection()
        conn.execute("INSERT INTO suppliers (name, note) VALUES (?, ?)", (name, note))
        conn.commit()
        conn.close()

        return redirect(url_for("suppliers_list"))

    return render_template("suppliers_add.html")


# =====================
# 仕入れ先削除
# =====================
@app.route("/suppliers/delete/<int:id>", methods=["POST"])
@login_required
def suppliers_delete(id):
    conn = get_db_connection()
    conn.execute("DELETE FROM suppliers WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect(url_for("suppliers_list"))



# =====================
# Flask起動
# =====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)


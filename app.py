from flask import Flask, render_template, request, redirect, session, url_for, send_file, jsonify
from db_config import get_connection
import pandas as pd
import io
from datetime import datetime

app = Flask(__name__)
app.secret_key = "your_secret_key"

def format_date(d):
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return None

# Login Route
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("""
            SELECT * FROM admin_login 
            WHERE (username = %s OR phone_no = %s) AND password = %s
        """, (username, username, password))
        
        user = cursor.fetchone()
        conn.close()
        
        if user:
            session["user"] = user["username"]
            return redirect(url_for("dashboard"))
        else:
            return render_template("index.html", error="Invalid credentials")
    
    return render_template("index.html")

# Dashboard Route
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Get available TVs
        cursor.execute("""
            SELECT id, serial_number, brand, size 
            FROM tv_inventory 
            WHERE status = 'available'
        """)
        available_tvs = cursor.fetchall()

        # 🔽 GET DISTINCT BRANDS
        cursor.execute("""
            SELECT DISTINCT brand 
            FROM tv_inventory 
            WHERE status = 'available'
            ORDER BY brand ASC
        """)
        tv_brands = [row['brand'] for row in cursor.fetchall()]

        # Get counts
        cursor.execute("SELECT COUNT(*) as total FROM tv_inventory")
        total_stock = cursor.fetchone()["total"]
        
        cursor.execute("SELECT COUNT(*) as available FROM tv_inventory WHERE status = 'available'")
        available_count = cursor.fetchone()["available"]
        
        cursor.execute("SELECT COUNT(*) as count FROM b2c_tv_sales")
        b2c_sales = cursor.fetchone()["count"]
        
        cursor.execute("SELECT COUNT(*) as count FROM b2b_tv_sales")
        b2b_sales = cursor.fetchone()["count"]
        
        # Get accessory counts
        cursor.execute("""
            SELECT 
                SUM(main_stock) as main_total,
                SUM(prabhu_stock) as prabhu_total,
                SUM(tamil_stock) as tamil_total
            FROM accessory_stock
        """)
        accessory_counts = cursor.fetchone()
        
        total_accessories = (accessory_counts['main_total'] or 0) + \
                            (accessory_counts['prabhu_total'] or 0) + \
                            (accessory_counts['tamil_total'] or 0)
        
        # 🔽 Get all accessory items for display
        cursor.execute("SELECT item_name, main_stock, prabhu_stock, tamil_stock FROM accessory_stock")
        accessory_items = cursor.fetchall()
        
        return render_template(
            "dashboard.html",
            available_tvs=available_tvs,
            tv_brands=tv_brands,  # ✅ Add this line to pass brands to HTML
            total_stock=total_stock,
            available_count=available_count,
            b2c_sales=b2c_sales,
            b2b_sales=b2b_sales,
            total_accessories=total_accessories,
            main_stock=accessory_counts['main_total'] or 0,
            prabhu_stock=accessory_counts['prabhu_total'] or 0,
            tamil_stock=accessory_counts['tamil_total'] or 0,
            accessory_items=accessory_items
        )
    finally:
        conn.close()


# Add Stock Route
@app.route("/add_stock")
def add_stock():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("add_stock.html")

# Add TV Route
@app.route("/add_tv", methods=["POST"])
def add_tv():
    if "user" not in session:
        return redirect(url_for("login"))
    
    serial = request.form.get("serial-number")
    brand = request.form.get("tv-brand")
    size = request.form.get("tv-size")
    
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Check if serial exists
        cursor.execute("SELECT id FROM tv_inventory WHERE serial_number = %s", (serial,))
        if cursor.fetchone():
            return "TV with this serial number already exists", 400
        
        # Add new TV
        cursor.execute("""
            INSERT INTO tv_inventory (serial_number, brand, size, status)
            VALUES (%s, %s, %s, 'available')
        """, (serial, brand, size))
        conn.commit()
        return redirect(url_for("dashboard"))
    except Exception as e:
        conn.rollback()
        return f"Error adding TV: {str(e)}", 400
    finally:
        conn.close()

# Add Accessory Route
@app.route("/add_accessory", methods=["POST"])
def add_accessory():
    if "user" not in session:
        return redirect(url_for("login"))
    
    name = request.form.get("accessory-name").strip()
    main = int(request.form.get("main-stock", 0))
    
    if main < 0:
        return "Stock value cannot be negative", 400
    
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Check if accessory exists
        cursor.execute("SELECT id FROM accessory_stock WHERE item_name = %s", (name,))
        existing = cursor.fetchone()
        
        if existing:
            # Update existing stock (only main stock now)
            cursor.execute("""
                UPDATE accessory_stock 
                SET main_stock = main_stock + %s
                WHERE item_name = %s
            """, (main, name))
        else:
            # Add new accessory (with zero stock for Prabhu and Tamil)
            cursor.execute("""
                INSERT INTO accessory_stock 
                (item_name, main_stock, prabhu_stock, tamil_stock)
                VALUES (%s, %s, 0, 0)
            """, (name, main))
            
        conn.commit()
        return redirect(url_for("dashboard"))
    except Exception as e:
        conn.rollback()
        return f"Error adding accessory: {str(e)}", 400
    finally:
        conn.close()

# Sales Submission Routes
@app.route("/submit_b2c_tv_sale", methods=["POST"])
def submit_b2c_tv_sale():
    if "user" not in session:
        return redirect(url_for("login"))
    
    serial = request.form.get("serial")
    if not serial:
        return "Serial number required", 400
    
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # ✅ Step 1: Fetch TV details (ID, brand, size)
        cursor.execute("""
            SELECT id, brand, size FROM tv_inventory 
            WHERE serial_number = %s AND status = 'available'
            FOR UPDATE
        """, (serial,))
        tv = cursor.fetchone()
        
        if not tv:
            return "TV not found or already sold", 404

        # ✅ Step 2: Insert sale using values from inventory
        cursor.execute("""
            INSERT INTO b2c_tv_sales (
                product_id, customer_name, phone, price, sale_date, warranty, brand, size
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            tv["id"],
            request.form.get("name"),
            request.form.get("phone"),
            request.form.get("price"),
            request.form.get("date"),
            request.form.get("warranty"),
            tv["brand"],
            tv["size"]
        ))
        
        # ✅ Step 3: Mark TV as sold
        cursor.execute("""
            UPDATE tv_inventory 
            SET status = 'sold' 
            WHERE id = %s
        """, (tv["id"],))
        
        conn.commit()
        return redirect(url_for("dashboard"))

    except Exception as e:
        conn.rollback()
        return f"Error processing sale: {str(e)}", 500

    finally:
        conn.close()

@app.route("/edit_tv", methods=["POST"])
def edit_tv():
    if "user" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        tv_id = request.form.get("id")
        serial = request.form.get("serial_number")
        brand = request.form.get("brand")
        size = request.form.get("size")

        if not all([tv_id, serial, brand, size]):
            return "Missing required fields", 400

        cursor.execute("""
            UPDATE tv_inventory 
            SET serial_number = %s, brand = %s, size = %s 
            WHERE id = %s
        """, (serial, brand, size, tv_id))
        conn.commit()
        return redirect(url_for("dashboard"))
    except Exception as e:
        conn.rollback()
        return f"Error updating TV: {str(e)}", 500
    finally:
        conn.close()


@app.route("/submit_b2b_tv_sale", methods=["POST"])
def submit_b2b_tv_sale():
    if "user" not in session:
        return redirect(url_for("login"))
    
    serial = request.form.get("serial")
    if not serial:
        return "Serial number required", 400
    
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # ✅ Step 1: Fetch TV details (ID, brand, size)
        cursor.execute("""
            SELECT id, brand, size FROM tv_inventory 
            WHERE serial_number = %s AND status = 'available'
            FOR UPDATE
        """, (serial,))
        tv = cursor.fetchone()
        
        if not tv:
            return "TV not found or already sold", 404

        # ✅ Step 2: Insert sale using values from inventory
        cursor.execute("""
            INSERT INTO b2b_tv_sales (
                product_id, business_name, phone, price, sale_date, warranty, brand, size
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            tv["id"],
            request.form.get("name"),
            request.form.get("phone"),
            request.form.get("price"),
            request.form.get("date"),
            request.form.get("warranty"),
            tv["brand"],
            tv["size"]
        ))
        
        # ✅ Step 3: Mark TV as sold
        cursor.execute("""
            UPDATE tv_inventory 
            SET status = 'sold' 
            WHERE id = %s
        """, (tv["id"],))
        
        conn.commit()
        return redirect(url_for("dashboard"))

    except Exception as e:
        conn.rollback()
        return f"Error processing sale: {str(e)}", 500

    finally:
        conn.close()

@app.route("/submit_accessory_sale", methods=["POST"])
def submit_accessory_sale():
    if "user" not in session:
        return redirect(url_for("login"))
    
    item_name = request.form.get("item")
    quantity = int(request.form.get("quantity", 0))
    labour = request.form.get("labour")
    
    if quantity <= 0:
        return "Quantity must be positive", 400
    
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Check stock availability
        if labour == "prabhu":  # Prabhu
            cursor.execute("""
                SELECT prabhu_stock FROM accessory_stock 
                WHERE item_name = %s AND prabhu_stock >= %s
                FOR UPDATE
            """, (item_name, quantity))
        elif labour == "tamil":  # Tamil
            cursor.execute("""
                SELECT tamil_stock FROM accessory_stock 
                WHERE item_name = %s AND tamil_stock >= %s
                FOR UPDATE
            """, (item_name, quantity))
        else:
            cursor.execute("""
                SELECT main_stock FROM accessory_stock 
                WHERE item_name = %s AND main_stock >= %s
                FOR UPDATE
            """, (item_name, quantity))
            
        stock = cursor.fetchone()
        if not stock:
            return "Insufficient stock", 400
        
        # Record sale
        cursor.execute("""
            INSERT INTO b2c_accessory_sales (
                item_name, quantity, customer_name, phone, 
                labour_name, price, sale_date
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            item_name,
            quantity,
            request.form.get("name"),
            request.form.get("phone"),
            labour,
            request.form.get("price"),
            request.form.get("date")
        ))
        
        # Update inventory
        if labour == "prabhu":  # Prabhu
            cursor.execute("""
                UPDATE accessory_stock 
                SET prabhu_stock = prabhu_stock - %s
                WHERE item_name = %s
            """, (quantity, item_name))
        elif labour == "tamil":  # Tamil
            cursor.execute("""
                UPDATE accessory_stock 
                SET tamil_stock = tamil_stock - %s
                WHERE item_name = %s
            """, (quantity, item_name))
        else:
            cursor.execute("""
                UPDATE accessory_stock 
                SET main_stock = main_stock - %s
                WHERE item_name = %s
            """, (quantity, item_name))
            
        conn.commit()
        return redirect(url_for("dashboard"))
    except Exception as e:
        conn.rollback()
        return f"Error processing sale: {str(e)}", 500
    finally:
        conn.close()

# Sales History Route (updated)
from datetime import datetime

@app.route("/sales_history")
def sales_history():
    if "user" not in session:
        return redirect(url_for("login"))

    raw_start = request.args.get("start_date", "").strip()
    raw_end = request.args.get("end_date", "").strip()
    search = request.args.get("search", "").strip()
    sort_order = request.args.get("sort", "DESC").upper()
    size_filter = request.args.get("size", "").strip()
    date_error = None

    def format_db_date(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            return None

    db_start_date = format_db_date(raw_start)
    db_end_date = format_db_date(raw_end)

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # TV B2C Query
        b2c_query = """
            SELECT 
                'B2C' AS type,
                b.id,
                b.customer_name AS name,
                b.phone,
                t.brand,
                t.size,
                t.serial_number,
                b.sale_date,
                b.warranty,
                b.price
            FROM b2c_tv_sales b
            JOIN tv_inventory t ON b.product_id = t.id
            WHERE 1=1
        """
        b2c_params = []

        if search:
            s = f"%{search}%"
            b2c_query += " AND (b.customer_name LIKE %s OR b.phone LIKE %s OR t.serial_number LIKE %s)"
            b2c_params += [s, s, s]

        if size_filter:
            b2c_query += " AND t.size = %s"
            b2c_params.append(size_filter)

        if db_start_date:
            b2c_query += " AND b.sale_date >= %s"
            b2c_params.append(db_start_date)

        if db_end_date:
            b2c_query += " AND b.sale_date <= %s"
            b2c_params.append(db_end_date)

        b2c_query += f" ORDER BY b.sale_date {sort_order}"
        cursor.execute(b2c_query, tuple(b2c_params))
        b2c_sales = cursor.fetchall()

        # TV B2B Query
        b2b_query = """
            SELECT 
                'B2B' AS type,
                b.id,
                b.business_name AS name,
                b.phone,
                t.brand,
                t.size,
                t.serial_number,
                b.sale_date,
                b.warranty,
                b.price
            FROM b2b_tv_sales b
            JOIN tv_inventory t ON b.product_id = t.id
            WHERE 1=1
        """
        b2b_params = []

        if search:
            s = f"%{search}%"
            b2b_query += " AND (b.business_name LIKE %s OR b.phone LIKE %s OR t.serial_number LIKE %s)"
            b2b_params += [s, s, s]

        if size_filter:
            b2b_query += " AND t.size = %s"
            b2b_params.append(size_filter)

        if db_start_date:
            b2b_query += " AND b.sale_date >= %s"
            b2b_params.append(db_start_date)

        if db_end_date:
            b2b_query += " AND b.sale_date <= %s"
            b2b_params.append(db_end_date)

        b2b_query += f" ORDER BY b.sale_date {sort_order}"
        cursor.execute(b2b_query, tuple(b2b_params))
        b2b_sales = cursor.fetchall()

        tv_sales = b2c_sales + b2b_sales
        tv_sales.sort(key=lambda x: x['sale_date'], reverse=(sort_order == "DESC"))

        # Accessory Query
        acc_query = """
            SELECT
                id,
                customer_name,
                phone,
                item_name,
                quantity,
                labour_name,
                sale_date,
                price
            FROM b2c_accessory_sales
            WHERE 1=1
        """
        acc_params = []

        if search:
            s = f"%{search}%"
            acc_query += " AND (customer_name LIKE %s OR phone LIKE %s OR item_name LIKE %s)"
            acc_params += [s, s, s]

        if db_start_date:
            acc_query += " AND sale_date >= %s"
            acc_params.append(db_start_date)

        if db_end_date:
            acc_query += " AND sale_date <= %s"
            acc_params.append(db_end_date)

        acc_query += f" ORDER BY sale_date {sort_order}"
        cursor.execute(acc_query, tuple(acc_params))
        accessory_sales = cursor.fetchall()

        return render_template(
            "sales_history.html",
            tv_sales=tv_sales,
            accessory_sales=accessory_sales,
            search=search,
            start_date=raw_start,
            end_date=raw_end,
            sort_order=sort_order,
            size_filter=size_filter,
            date_error=date_error
        )

    except Exception as e:
        return f"Error: {str(e)}", 500
    finally:
        conn.close()

      
# Export Routes
@app.route("/export_tv_sales")
def export_tv_sales():
    if "user" not in session:
        return redirect(url_for("login"))

    search = request.args.get("search", "").strip()
    size_filter = request.args.get("size", "")
    start_date = format_date(request.args.get("start_date", ""))
    end_date = format_date(request.args.get("end_date", ""))

    sort_order = request.args.get("sort", "DESC").upper()

    if sort_order not in ("ASC", "DESC"):
        sort_order = "DESC"

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        tv_query = """
        SELECT 
            'B2C' AS type,
            b.customer_name AS name,
            b.phone,
            t.brand,
            t.size,
            t.serial_number,
            b.sale_date,
            b.warranty,
            b.price
        FROM b2c_tv_sales b
        JOIN tv_inventory t ON b.product_id = t.id
        WHERE 1=1
        """
        params = []

        if search:
            tv_query += """
            AND (
                b.customer_name LIKE %s OR 
                b.phone LIKE %s OR 
                t.serial_number LIKE %s
            )
            """
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])

        if size_filter:
            tv_query += " AND t.size = %s"
            params.append(size_filter)

        if start_date:
            tv_query += " AND b.sale_date >= %s"
            params.append(start_date)

        if end_date:
            tv_query += " AND b.sale_date <= %s"
            params.append(end_date)

        tv_query += """
        UNION ALL
        SELECT 
            'B2B' AS type,
            b.business_name AS name,
            b.phone,
            t.brand,
            t.size,
            t.serial_number,
            b.sale_date,
            b.warranty,
            b.price
        FROM b2b_tv_sales b
        JOIN tv_inventory t ON b.product_id = t.id
        WHERE 1=1
        """

        if search:
            tv_query += """
            AND (
                b.business_name LIKE %s OR 
                b.phone LIKE %s OR 
                t.serial_number LIKE %s
            )
            """
            params.extend([search_param, search_param, search_param])

        if size_filter:
            tv_query += " AND t.size = %s"
            params.append(size_filter)

        if start_date:
            tv_query += " AND b.sale_date >= %s"
            params.append(start_date)

        if end_date:
            tv_query += " AND b.sale_date <= %s"
            params.append(end_date)

        tv_query += f" ORDER BY sale_date {sort_order}"

        cursor.execute(tv_query, tuple(params))
        tv_sales = cursor.fetchall()

        df = pd.DataFrame(tv_sales)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="TV Sales")

        output.seek(0)
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="tv_sales_export.xlsx"
        )
    finally:
        conn.close()


@app.route("/export_accessory_sales")
def export_accessory_sales():
    if "user" not in session:
        return redirect(url_for("login"))

    search = request.args.get("search", "").strip()
    start_date = format_date(request.args.get("start_date", ""))
    end_date = format_date(request.args.get("end_date", ""))

    sort_order = request.args.get("sort", "DESC").upper()

    if sort_order not in ("ASC", "DESC"):
        sort_order = "DESC"

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        accessory_query = """
        SELECT
            customer_name,
            phone,
            item_name,
            quantity,
            labour_name,
            sale_date,
            price
        FROM b2c_accessory_sales
        WHERE 1=1
        """
        params = []

        if search:
            accessory_query += """
            AND (
                customer_name LIKE %s OR
                phone LIKE %s OR
                item_name LIKE %s
            )
            """
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])

        if start_date:
            accessory_query += " AND sale_date >= %s"
            params.append(start_date)

        if end_date:
            accessory_query += " AND sale_date <= %s"
            params.append(end_date)

        accessory_query += f" ORDER BY sale_date {sort_order}"

        cursor.execute(accessory_query, tuple(params))
        accessory_sales = cursor.fetchall()

        df = pd.DataFrame(accessory_sales)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Accessory Sales")

        output.seek(0)
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="accessory_sales_export.xlsx"
        )
    finally:
        conn.close()


# Serial Number Validation
@app.route("/get_available_serials")
def get_available_serials():
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT serial_number 
            FROM tv_inventory 
            WHERE status = 'available'
            ORDER BY serial_number
        """)
        serials = [row[0] for row in cursor.fetchall()]
        return jsonify({"serials": serials})
    finally:
        conn.close()
        
@app.route("/transfer_accessory", methods=["POST"])
def transfer_accessory():
    if "user" not in session:
        return redirect(url_for("login"))
    
    item_name = request.form.get("item_name")
    from_location = request.form.get("from")
    to_location = request.form.get("to")
    quantity = int(request.form.get("quantity", 0))
    
    if quantity <= 0:
        return "Quantity must be positive", 400
    
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Verify sufficient stock
        cursor.execute(f"""
            SELECT {from_location}_stock FROM accessory_stock 
            WHERE item_name = %s AND {from_location}_stock >= %s
            FOR UPDATE
        """, (item_name, quantity))
        
        if not cursor.fetchone():
            return f"Insufficient stock in {from_location}", 400
        
        # Perform transfer
        cursor.execute(f"""
            UPDATE accessory_stock 
            SET {from_location}_stock = {from_location}_stock - %s,
                {to_location}_stock = {to_location}_stock + %s
            WHERE item_name = %s
        """, (quantity, quantity, item_name))
        
        conn.commit()
        return redirect(url_for("dashboard"))
    except Exception as e:
        conn.rollback()
        return f"Error transferring stock: {str(e)}", 500
    finally:
        conn.close()

@app.route('/delete_sale', methods=['POST'])
def delete_sale():
    if 'user' not in session:
        return redirect(url_for('login'))

    sale_id = request.form.get('sale_id')
    sale_type = request.form.get('sale_type')

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if sale_type in ['b2b_tv', 'b2c_tv']:
            # Determine table and get product_id
            table = 'b2b_tv_sales' if sale_type == 'b2b_tv' else 'b2c_tv_sales'
            
            # Get sale details including product_id
            cursor.execute(f"SELECT product_id FROM {table} WHERE id = %s", (sale_id,))
            sale = cursor.fetchone()
            
            if not sale:
                return "Sale not found", 404

            # Update TV inventory status back to 'available'
            cursor.execute("""
                UPDATE tv_inventory 
                SET status = 'available' 
                WHERE id = %s
            """, (sale['product_id'],))
            
            # Delete the sale record
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", (sale_id,))

        elif sale_type == 'b2c_accessory':
            # Get accessory sale details
            cursor.execute("""
                SELECT item_name, quantity, labour_name 
                FROM b2c_accessory_sales 
                WHERE id = %s
            """, (sale_id,))
            sale = cursor.fetchone()
            
            if not sale:
                return "Sale not found", 404

            # Determine which stock location to update based on labour_name
            if sale['labour_name'] == 'prabhu':  # Prabhu
                stock_column = 'prabhu_stock'
            elif sale['labour_name'] == 'tamil':  # Tamil
                stock_column = 'tamil_stock'
            else:  # Main stock
                stock_column = 'main_stock'

            # Update the appropriate stock location
            cursor.execute(f"""
                UPDATE accessory_stock 
                SET {stock_column} = {stock_column} + %s 
                WHERE item_name = %s
            """, (sale['quantity'], sale['item_name']))
            
            # Delete the sale record
            cursor.execute("DELETE FROM b2c_accessory_sales WHERE id = %s", (sale_id,))

        conn.commit()
        return redirect(url_for('sales_history'))

    except Exception as e:
        conn.rollback()
        return f"Error deleting sale: {str(e)}", 500

    finally:
        cursor.close()
        conn.close()

@app.route("/search_items")
def search_items():
    query = request.args.get("query", "").strip().lower()
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT item_name FROM accessory_stock 
            WHERE LOWER(item_name) LIKE %s
            ORDER BY item_name ASC
            LIMIT 10
        """, (f"%{query}%",))
        results = [row["item_name"] for row in cursor.fetchall()]
        return jsonify({"results": results})
    finally:
        conn.close()


# Logout Route
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True)
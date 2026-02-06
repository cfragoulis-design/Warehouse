# === Central Ready helpers ===
# Add this block at the END of services.py

def get_central_ready(conn):
    row = conn.execute("""
        SELECT central_ready, central_ready_at
        FROM app_state
        WHERE id = 1
    """).fetchone()
    return {
        "central_ready": row[0] if row else False,
        "central_ready_at": row[1] if row else None,
    }

def set_central_ready(conn):
    conn.execute("""
        UPDATE app_state
        SET central_ready = TRUE,
            central_ready_at = NOW()
        WHERE id = 1
    """)

def clear_central_ready(conn):
    conn.execute("""
        UPDATE app_state
        SET central_ready = FALSE
        WHERE id = 1
    """)

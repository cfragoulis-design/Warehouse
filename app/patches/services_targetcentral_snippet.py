# app/services.py changes

# A) product_create(...) signature add:
#    target_central: int = Form(0)
# and in Product(...) add:
#    target_central=int(target_central or 0),

# B) product_update(...) signature add:
#    target_central: int = Form(0)
# and set:
#    product.target_central = int(target_central or 0)

# C) stock_view query: include Product.target_central in select(...)
# and in item dict add:
#    "target_central": int(r.target_central or 0),
#    "pending_central": max(0, int(r.target_central or 0) - int(c)),
#
# NOTE:
# - Use central_qty 'c' (already computed Decimal) and cast to int for pending.
# - Pending is only displayed when > 0.

# app/services.py - copy/paste guide

# 1) product_create(...) signature add:
#    min_stock: int = Form(0)
# and in Product(...) add:
#    min_stock=int(min_stock or 0),

# 2) product_update(...) signature add:
#    min_stock: int = Form(0)
# and set:
#    product.min_stock = int(min_stock or 0)

# 3) stock_view query: include Product.min_stock in select (...)
# and in item dict add:
#    "min_stock": int(r.min_stock or 0),
#    "is_low": (int(r.min_stock or 0) > 0) and ((c + w) <= int(r.min_stock or 0)),

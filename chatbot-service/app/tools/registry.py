"""Single place that collects all LangChain tools exposed to the LLM."""

from app.tools.add_to_cart import add_to_cart
from app.tools.cancel_order import cancel_order
from app.tools.compare_products import compare_products
from app.tools.escalate import escalate_to_admin
from app.tools.get_menu import get_menu
from app.tools.get_product_detail import get_product_detail
from app.tools.order_status import get_order_status
from app.tools.payment_status import check_payment_status
from app.tools.reports import business_analytics, financial_report

ALL_TOOLS = [
    get_menu,
    get_product_detail,
    compare_products,
    add_to_cart,
    get_order_status,
    check_payment_status,
    cancel_order,
    escalate_to_admin,
    financial_report,
    business_analytics,
]

TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}

from django.views.generic import TemplateView

class LandingPageView(TemplateView):
    template_name = "landing_page/index.html"

class OfflineView(TemplateView):
    template_name = "landing_page/offline.html"

class ServiceWorkerView(TemplateView):
    template_name = "landing_page/sw.js"
    content_type = "application/javascript"

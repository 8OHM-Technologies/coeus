from django.db import models

class SiteContactForm(models.Model):
    """
    Model to store contact form submissions from the marketing landing page.
    """
    name = models.CharField(max_length=255)
    email = models.EmailField()
    division = models.CharField(max_length=100)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "site_contact_form"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} - {self.email} ({self.division}) at {self.created_at}"

import json
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from .models import SiteContactForm

@csrf_exempt
def send_website_enquiry(request):
    """
    API endpoint to handle landing page contact form submissions.
    Supports OPTIONS (CORS preflight) and POST requests.
    """
    # Handle CORS preflight request
    if request.method == "OPTIONS":
        response = HttpResponse()
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type, X-CSRFToken"
        response["Access-Control-Max-Age"] = "86400"
        return response

    if request.method != "POST":
        response = JsonResponse({"status": "error", "message": "Method not allowed"}, status=405)
        response["Access-Control-Allow-Origin"] = "*"
        return response

    # Parse incoming payload
    try:
        if request.content_type == "application/json":
            data = json.loads(request.body.decode("utf-8"))
        else:
            data = request.POST
    except Exception as e:
        response = JsonResponse({"status": "error", "message": f"Malformed request: {str(e)}"}, status=400)
        response["Access-Control-Allow-Origin"] = "*"
        return response

    # Extract and clean fields
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    division = data.get("division", "").strip()
    message = data.get("message", "").strip()

    # Validate fields
    if not name:
        msg = "Name is required."
    elif not email or "@" not in email:
        msg = "A valid email address is required."
    elif not division:
        msg = "Please select a division."
    elif not message:
        msg = "Message is required."
    else:
        msg = None

    if msg:
        response = JsonResponse({"status": "error", "message": msg}, status=400)
        response["Access-Control-Allow-Origin"] = "*"
        return response

    try:
        # Create and save submission
        SiteContactForm.objects.create(
            name=name,
            email=email,
            division=division,
            message=message
        )
        response = JsonResponse({
            "status": "success",
            "message": "Thank you for your enquiry. We will get back to you shortly!"
        }, status=201)
    except Exception as e:
        response = JsonResponse({
            "status": "error",
            "message": f"An error occurred while saving the enquiry: {str(e)}"
        }, status=500)

    response["Access-Control-Allow-Origin"] = "*"
    return response

from datetime import date
import pytest
from django.urls import reverse
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application
from rest_framework import status
from rest_framework.test import APIClient
from extracted_data.models import Entity, Target, ExtractedRecord

User = get_user_model()


@pytest.mark.django_db
def test_unauthenticated_api_access():
    client = APIClient()
    urls = [
        reverse("entity-list"),
        reverse("target-list"),
        reverse("extractedrecord-list"),
    ]
    for url in urls:
        response = client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
def test_authenticated_api_access():
    client = APIClient()

    # Create test user
    user = User.objects.create_user(username="testapiuser", password="securepassword123")

    # Create OAuth2 Application
    app = Application.objects.create(
        name="Test App",
        user=user,
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
        client_secret="test-client-secret-123",
    )

    # Request access token via the /o/token/ endpoint
    import base64
    token_url = reverse("oauth2_provider:token")
    credentials = f"{app.client_id}:test-client-secret-123"
    base64_credentials = base64.b64encode(credentials.encode()).decode()
    payload = {
        "grant_type": "client_credentials",
    }
    response = client.post(
        token_url,
        payload,
        HTTP_AUTHORIZATION=f"Basic {base64_credentials}"
    )
    assert response.status_code == status.HTTP_200_OK
    token_data = response.json()
    assert "access_token" in token_data
    access_token = token_data["access_token"]

    # Create test data
    entity = Entity.objects.create(name="Acme Corp", identifier="ACME")
    target = Target.objects.create(entity=entity, target_name="Main Site", location="https://acme.com")
    record = ExtractedRecord.objects.create(
        target=target,
        document_date=date.today(),
        record_type="Financial Summary",
        data={"revenue": 1000000},
        requires_human_review=False,
    )

    # Access API with token
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    # 1. Test Entity API
    response = client.get(reverse("entity-list"))
    assert response.status_code == status.HTTP_200_OK
    results = response.json()
    assert len(results) == 1
    assert results[0]["name"] == "Acme Corp"

    # 2. Test Target API
    response = client.get(reverse("target-list"))
    assert response.status_code == status.HTTP_200_OK
    results = response.json()
    assert len(results) == 1
    assert results[0]["target_name"] == "Main Site"

    # 3. Test ExtractedRecord API
    response = client.get(reverse("extractedrecord-list"))
    assert response.status_code == status.HTTP_200_OK
    results = response.json()
    assert len(results) == 1
    assert results[0]["record_type"] == "Financial Summary"

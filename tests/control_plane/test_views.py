import pytest
import json
from django.urls import reverse
from django.test import RequestFactory
from django.http import JsonResponse
from pipelines.models import PipelineConfiguration
from pipelines.views import active_pipelines_api, launch_selector_lab, sync_selectors_from_lab

@pytest.mark.django_db
def test_active_pipelines_api(rf):
    # Create active and inactive configurations
    PipelineConfiguration.objects.create(name="Active Config", start_url="https://active.com", is_active=True)
    PipelineConfiguration.objects.create(name="Inactive Config", start_url="https://inactive.com", is_active=False)

    request = rf.get("/api/pipelines/active/")
    response = active_pipelines_api(request)

    assert response.status_code == 200
    data = json.loads(response.content)
    assert "pipelines" in data
    assert len(data["pipelines"]) == 1
    assert data["pipelines"][0]["name"] == "Active Config"


@pytest.mark.django_db
def test_launch_selector_lab(rf, mocker):
    config = PipelineConfiguration.objects.create(
        name="Lab Test",
        start_url="https://lab.com",
    )
    
    mock_popen = mocker.patch("subprocess.Popen")
    
    request = rf.post(f"/pipelines/launch/{config.pk}/")
    response = launch_selector_lab(request, config.pk)
    
    assert response.status_code == 302
    assert response.url == "http://localhost:8081/vnc.html?autoconnect=true"
    
    mock_popen.assert_called_once()
    args = mock_popen.call_args[0][0]
    assert "coeus_selector_lab" in args
    assert "playwright codegen https://lab.com" in args[-1]


@pytest.mark.django_db
def test_sync_selectors_from_lab_file_not_found(rf, mocker):
    config = PipelineConfiguration.objects.create(
        name="Sync Test 1",
        start_url="https://sync1.com",
    )
    
    mocker.patch("os.path.exists", return_value=False)
    
    request = rf.post(f"/pipelines/sync/{config.pk}/")
    response = sync_selectors_from_lab(request, config.pk)
    
    assert response.status_code == 200
    data = json.loads(response.content)
    assert data["status"] == "error"
    assert data["message"] == "No discovery file found."

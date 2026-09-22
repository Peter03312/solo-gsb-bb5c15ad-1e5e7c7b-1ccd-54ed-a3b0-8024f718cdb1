import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在导入 app 之前指定测试数据库与路网
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ROADS_GEOJSON"] = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "data", "roads.geojson"
)


@pytest.fixture(scope="session")
def network():
    from app.mapmatch.road_network import RoadNetwork

    return RoadNetwork.from_geojson(os.environ["ROADS_GEOJSON"])

import json
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.domain.truck_demo import demo_correction_batch, demo_truck_events
from src.models.identity import User
from src.models.source import Dataset
from src.models.trucks import CorrectionBatch
from src.services.datasets import save
from src.services import reconciliation as truck_service
from src.utils.security import hash_password


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        event_result = truck_service.import_events(db, demo_truck_events(), 'seed')
        batch_applied = False
        batch = db.scalar(select(CorrectionBatch).where(CorrectionBatch.batch_seq == 1))
        if batch is None:
            demo = demo_correction_batch()
            _, batch_applied = truck_service.submit_correction_batch(
                db, demo['batch_seq'], demo['note'], demo['corrections'], 'seed')
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created,
                'truck_events': event_result,
                'correction_batch_applied': batch_applied}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))

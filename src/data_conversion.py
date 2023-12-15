import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pyarrow.compute as pc
from pyarrow.interchange import from_dataframe
import json
import polars as pl
import numpy as np
import pickle
import gzip
import sqlite3
from dataclasses import dataclass

@dataclass
class Dataset:
    steps: ... 
    frames: ...
    types: ...
    nodes: ...

def read_data(path: str) -> dict:
    file = open(path)
    data = json.load(file)
    file.close()
    return data

def to_table(data: dict) -> pl.DataFrame:
    table = [];
    for step_name, step in data.items():
        for frame_num, frame in step.items():
            print(step_name, frame_num)
            for val_type, nodes in frame.items():
                for node in nodes:
                    node_padded = [np.NaN]*6
                    if isinstance(node, float):
                        node = [node]
                    node_padded[:len(node)] = node

                    row = [
                        step_name,
                        frame_num,
                        val_type,
                    ] + node_padded

                    table.append(row)
    col_names = ['step_name', 'frame_num', 'val_type', 'val1', 'val2', 'val3', 'val4', 'val5', 'val6']
    return pl.DataFrame(table, col_names)

def to_dataset(data) -> Dataset:
    table_steps = []
    table_nodes = []
    table_frames = []
    types_unique = {}
    types_id = -1
    for step_id, step_name in enumerate(data.keys()):
        table_steps.append([step_id, step_name])
        step = data[step_name]

        for frame_id, frame_num in enumerate(step.keys()):
            table_frames.append([frame_id, step_id, frame_num])

            for node_type, nodes in step[frame_num].items():
                type_id = types_unique.get(node_type)
                if type_id is None:
                    types_id += 1
                    types_unique[node_type] = types_id
                    type_id = types_id

                for node_id, node in enumerate(nodes):
                    node_padded = [node_id, type_id, frame_id] + [np.NaN]*5
                    if isinstance(node, float):
                        node = [node]
                    node_padded[3:len(node)] = node

                    table_nodes.append(node_padded)

    table_types = [[id, type_name] for type_name, id in types_unique.items()]

    return Dataset(
        steps=pl.DataFrame(table_steps, ['step_id', 'step_name']),
        types=pl.DataFrame(table_types, ['type_id', 'type_name']),
        frames=pl.DataFrame(table_frames, ['frame_id', 'step_id', 'frame_num']),
        nodes=pl.DataFrame(table_nodes, ['node_id', 'type_id', 'frame_id', 'val1', 'val2', 'val3', 'val4', 'val5', 'val6']),
    )

class ParquetTable:
    @staticmethod
    def convert(in_path: str, out_path: str):
        data = read_data(in_path)
        table = to_table(data)
        table.write_parquet(out_path)

    @staticmethod
    def read_batch(path, steps, types):
        table = pl.read_parquet(path)

        samples = table.filter(
            pl.col('step_name').is_in(steps['step_name']) & 
            pl.col('val_type').is_in(types['type_name'])
        )
        print(samples.shape)

class ParquetDataset:
    @staticmethod
    def convert(in_path: str, out_dir: str):
        data = read_data(in_path)
        dataset = to_dataset(data)
        dataset.steps.write_parquet(out_dir+'/steps.parquet')
        dataset.frames.write_parquet(out_dir+'/frames.parquet')
        dataset.types.write_parquet(out_dir+'/types.parquet')
        dataset.nodes.write_parquet(out_dir+'/nodes.parquet')

    @staticmethod
    def read_batch(path, steps, types):
        table_frames = pl.read_parquet(path+'/frames.parquet')\
            .filter(pl.col('step_id').is_in(steps['step_id']))

        samples = pl.read_parquet(path+'/nodes.parquet')\
            .filter(
                pl.col('type_id').is_in(types['type_id']) &
                pl.col('frame_id').is_in(table_frames['frame_id'])
            )
        print(samples.shape)

class CsvTable:
    @staticmethod
    def convert(in_path: str, out_path: str):
        data = read_data(in_path)
        table = to_table(data) 
        table.write_csv(out_path)

    @staticmethod
    def read_batch(path, steps, types):
        table = pl.read_csv(path, dtypes={ 'frame_num': pl.Utf8 })

        samples = table\
            .filter(
                pl.col('step_name').is_in(steps['step_name']) & 
                pl.col('val_type').is_in(types['type_name'])
            )
        print(samples.shape)

class SqliteTable:
    @staticmethod
    def convert(in_path: str, out_path: str):
        data = read_data(in_path)
        table = to_table(data) 
        conn = sqlite3.connect(out_path)

        with conn:
            sql = '''
                CREATE TABLE IF NOT EXISTS data(
                    id integer PRIMARY KEY,
                    step_name text,
                    frame_num text,
                    type_name text,
                    val1 real,
                    val2 real,
                    val3 real,
                    val4 real,
                    val5 real,
                    val6 real
                );
            '''
            cur = conn.cursor()
            cur.execute(sql)
            conn.commit()

            for row in table.rows():
                sql = '''
                    INSERT INTO data(
                        step_name,
                        frame_num,
                        type_name,
                        val1,
                        val2,
                        val3,
                        val4,
                        val5,
                        val6
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                '''
                cur = conn.cursor()
                cur.execute(sql, row)
            conn.commit()

    @staticmethod
    def read_batch(path, steps: pl.DataFrame, types):
        step_list = steps['step_name'].str.concat('","').to_list()[0]
        type_list = types['type_name'].str.concat('","').to_list()[0]

        conn = sqlite3.connect(path)
        with conn:
            sql = f'''
                select 
                    *
                from
                    data
                where
                    step_name in ("{step_list}")
                and type_name in ("{type_list}")
            '''
            cur = conn.cursor()
            cur.execute(sql)
            samples = cur.fetchall()
            conn.commit()

        print(pl.DataFrame(np.array(samples)).shape)

def save_as_compressed(data, path: str):
    json_data = json.dumps(data)
    encoded = json_data.encode('utf-8')
    compressed = gzip.compress(encoded)
    file = open(path, 'wb')
    file.write(compressed)
    file.close()

class ArrowTable:
    @staticmethod
    def convert(in_path: str, out_path: str):
        data = read_data(in_path)
        table = to_table(data)
        table = from_dataframe(table)
        pq.write_table(table, out_path)

    @staticmethod
    def read_batch(path, steps, types):
        table = pq.read_table(path)
        samples = table\
            .filter(
                pc.field('step_name').isin(steps['step_name']) &
                pc.field('val_type').isin(types['type_name'])
            )

        print(samples.shape)

class ArrowDataset:
    @staticmethod
    def convert(in_path: str, out_path: str):
        data = read_data(in_path)
        table = to_table(data)
        table = from_dataframe(table)
        part = ds.partitioning(pa.schema([
            ('step_name', pa.large_string()),
            ('frame_num', pa.int32()),
        ]), flavor=None)

        ds.write_dataset(table, out_path, format='parquet', partitioning=part, existing_data_behavior='overwrite_or_ignore')

    @staticmethod
    def read_batch(path, steps, types):
        dataset = ds.dataset(path)
        samples = dataset\
            .filter(
                pc.field('step_id').isin(steps['step_id']) &
                pc.field('type_id').isin(types['type_id'])
            )

        samples = pl.from_arrow(samples.to_table())
        print(samples.shape)


def read_batch(path: str, storage):
    data = read_data('data/data.json')
    dataset = to_dataset(data)

    steps = dataset.steps.sample(fraction=0.67, seed=0)
    types = dataset.types.sample(fraction=0.67, seed=0)
    storage.read_batch(path, steps, types)


if __name__ == '__main__':
    #CsvTable.convert('data/data.json', 'data/table.csv')
    read_batch('data/table.csv', CsvTable)

    #ParquetTable.convert('data/data.json', 'data/table.parquet')
    read_batch('data/table.parquet', ParquetTable)

    #SqliteTable.convert('data/data.json', 'data/table.sqlite')
    read_batch('data/table.sqlite', SqliteTable)

    #ParquetDataset.convert('data/data.json', 'data/parquet')
    read_batch('data/parquet', ParquetDataset)

    #ArrowDataset.convert('data/data.json', 'data/arrow-dataset')
    #read_batch('data/arrow-dataset', ArrowDataset)

    #ArrowTable.convert('data/data.json', 'data/arrow-table')
    read_batch('data/arrow-table', ArrowTable)

    #data = read_data('data/data.json')
    #table = to_table(data)
    #save_table_as_csv(table, 'data/data.csv')
    #save_table_as_parquet(table, 'data/data.parquet')
    #save_as_pickle(table, 'data/table.pickle')
    #save_as_pickle(data, 'data/data.pickle')
    #save_as_compressed(data, 'data/data.gz')
    #save_table_as_sqlite(table, 'data/data.sqlite')
    #save_table_as_arrow_table(table, 'data/arrow-table')
    #save_table_as_arrow_dataset(table, 'data/arrow-dataset')

    #dataset = to_dataset(data)
    #dataset.to_parquet('data/polars')

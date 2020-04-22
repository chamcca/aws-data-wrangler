"""PyTorch Module."""
import logging
import io
import os
import tarfile
import pathlib
import re
from io import BytesIO
from typing import Any, Callable, Iterator, List, Optional, Tuple, Union

import boto3  # type: ignore
import numpy as np  # type: ignore
import sqlalchemy  # type: ignore
import torch  # type: ignore
import torchaudio  # type: ignore
from PIL import Image  # type: ignore
from torch.utils.data.dataset import Dataset, IterableDataset  # type: ignore
from torchvision.transforms.functional import to_tensor  # type: ignore

from awswrangler import _utils, db, s3

_logger: logging.Logger = logging.getLogger(__name__)


class _BaseS3Dataset:
    """PyTorch Amazon S3 Map-Style Dataset."""

    def __init__(
        self, path: Union[str, List[str]], suffix: Optional[str] = None, boto3_session: Optional[boto3.Session] = None
    ):
        """PyTorch Map-Style S3 Dataset.

        Parameters
        ----------
        path : Union[str, List[str]]
            S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
        boto3_session : boto3.Session(), optional
            Boto3 Session. The default boto3 session will be used if boto3_session receive None.

        Returns
        -------
        torch.utils.data.Dataset

        """
        super().__init__()
        self._session = _utils.ensure_session(session=boto3_session)
        self._paths: List[str] = s3._path2list(  # pylint: disable=protected-access
            path=path, suffix=suffix, boto3_session=self._session
        )

    def _fetch_data(self, path: str):
        """Add parquet and csv support"""
        bucket, key = _utils.parse_path(path=path)
        buff = BytesIO()
        client_s3: boto3.client = _utils.client(service_name="s3", session=self._session)
        client_s3.download_fileobj(Bucket=bucket, Key=key, Fileobj=buff)
        buff.seek(0)
        return buff

    @staticmethod
    def _load_data(data: io.BytesIO, path: str):
        if path.endswith('.tar.gz') or path.endswith('.tgz'):
            pass
            # tarfile.open(fileobj=data)
            # tar = tarfile.open(fileobj=data)
            # for member in tar.getmembers():
            #     print('member', member)
        elif path.endswith('.pt'):
            data = torch.load(data)
        return data


class _ListS3Dataset(_BaseS3Dataset, Dataset):
    """PyTorch Amazon S3 Map-Style List Dataset."""

    def __init__(
        self, path: Union[str, List[str]], suffix: Optional[str] = None, boto3_session: Optional[boto3.Session] = None
    ):
        """PyTorch Map-Style List S3 Dataset.

        Each file under path would be handle as a single tensor.

        Parameters
        ----------
        path : Union[str, List[str]]
            S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
        boto3_session : boto3.Session(), optional
            Boto3 Session. The default boto3 session will be used if boto3_session receive None.

        Returns
        -------
        torch.utils.data.Dataset

        """
        super(_ListS3Dataset, self).__init__(path, suffix, boto3_session)

    def __getitem__(self, index):
        path = self._paths[index]
        data = self._fetch_data(path)
        return [self._data_fn(data), self._label_fn(path)]

    def __len__(self):
        return len(self._paths)

    def _data_fn(self, data):
        pass

    def _label_fn(self, path: str):
        pass


class _S3PartitionedDataset(_ListS3Dataset):
    """PyTorch Amazon S3 Map-Style Partitioned Dataset."""

    def _label_fn(self, path: str):
        return int(re.findall(r"/(.*?)=(.*?)/", path)[-1][1])


class S3FilesDataset(_BaseS3Dataset, Dataset):
    """PyTorch Amazon S3 Files Map-Style Dataset."""

    def __init__(
        self, path: Union[str, List[str]], suffix: Optional[str] = None, boto3_session: Optional[boto3.Session] = None
    ):
        """PyTorch S3 Files Map-Style Dataset.

        Each file under Amazon S3 path would be handled as a batch of tensors.
        All files will be loaded to memory since random access is needed.

        Parameters
        ----------
        path : Union[str, List[str]]
            S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
        boto3_session : boto3.Session(), optional
            Boto3 Session. The default boto3 session will be used if boto3_session receive None.

        Returns
        -------
        torch.utils.data.Dataset

        """
        super(S3FilesDataset, self).__init__(path, suffix, boto3_session)

    def _download_files(self):
        self._data = []
        for path in self._paths:
            data = self._fetch_data(path)
            data = self._load_data(data, path)
            self._data.append(data)

        self.data = torch.tensor(self._data)

    def __getitem__(self, index):
        return self._data[index]

    def __len__(self):
        return len(self._data)


class LambdaS3Dataset(_ListS3Dataset):
    """PyTorch Amazon S3 Lambda Map-Style Dataset."""

    def __init__(
        self,
        path: Union[str, List[str]],
        data_fn: Callable,
        label_fn: Callable,
        suffix: Optional[str] = None,
        boto3_session: Optional[boto3.Session] = None,
    ):
        """PyTorch S3 Lambda Dataset.

        Parameters
        ----------
        path : Union[str, List[str]]
            S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
        boto3_session : boto3.Session(), optional
            Boto3 Session. The default boto3 session will be used if boto3_session receive None.

        Returns
        -------
        torch.utils.data.Dataset

        Examples
        --------
        >>> import awswrangler as wr
        >>> import boto3
        >>> _data_fn = lambda x: torch.tensor(x)
        >>> _label_fn = lambda x: x.split('.')[-1]
        >>> ds = wr.torch.LambdaS3Dataset('s3://bucket/path', boto3.Session(), _data_fn=_data_fn, _label_fn=_label_fn)

        """
        super(LambdaS3Dataset, self).__init__(path, suffix, boto3_session)
        self._data_func = data_fn
        self._label_func = label_fn

    def _label_fn(self, path: str):
        return self._label_func(path)

    def _data_fn(self, data):
        print(type(data))
        return self._data_func(data)


class AudioS3Dataset(_S3PartitionedDataset):
    """PyTorch S3 Audio Dataset."""

    def __init__(
        self,
        path: Union[str, List[str]],
        cache_dir: str = "/tmp/",
        suffix: Optional[str] = None,
        boto3_session: Optional[boto3.Session] = None,
    ):
        """PyTorch S3 Audio Dataset.

        Assumes audio files are stored with the following structure:

        bucket
        ├── class=0
        │   ├── audio0.wav
        │   └── audio1.wav
        └── class=1
            ├── audio2.wav
            └── audio3.wav

        Parameters
        ----------
        path : Union[str, List[str]]
            S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
        boto3_session : boto3.Session(), optional
            Boto3 Session. The default boto3 session will be used if boto3_session receive None.

        Returns
        -------
        torch.utils.data.Dataset

        Examples
        --------
        >>> import awswrangler as wr
        >>> import boto3
        >>> ds = wr.torch.AudioS3Dataset('s3://bucket/path', boto3.Session())

        """
        super(AudioS3Dataset, self).__init__(path, suffix, boto3_session)
        self._cache_dir: str = cache_dir[:-1] if cache_dir.endswith("/") else cache_dir

    def _data_fn(self, filename: str) -> Tuple[Any, Any]:  # pylint: disable=arguments-differ
        waveform, sample_rate = torchaudio.load(filename)
        os.remove(path=filename)
        return waveform, sample_rate

    def _fetch_data(self, path: str) -> str:
        bucket, key = _utils.parse_path(path=path)
        filename: str = f"{self._cache_dir}/{bucket}/{key}"
        pathlib.Path(filename).parent.mkdir(parents=True, exist_ok=True)
        client_s3 = _utils.client(service_name="s3", session=self._session)
        client_s3.download_file(Bucket=bucket, Key=key, Filename=filename)
        return filename


class ImageS3Dataset(_S3PartitionedDataset):
    """PyTorch S3 Image Dataset."""

    def __init__(self, path: Union[str, List[str]], suffix: str, boto3_session: boto3.Session):
        """PyTorch S3 Image Dataset.

        Assumes Images are stored with the following structure:

        bucket
        ├── class=0
        │   ├── img0.jpeg
        │   └── img1.jpeg
        └── class=1
            ├── img2.jpeg
            └── img3.jpeg

        Parameters
        ----------
        path : Union[str, List[str]]
            S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
        boto3_session : boto3.Session(), optional
            Boto3 Session. The default boto3 session will be used if boto3_session receive None.

        Returns
        -------
        torch.utils.data.Dataset

        Examples
        --------
        >>> import awswrangler as wr
        >>> import boto3
        >>> ds = wr.torch.ImageS3Dataset('s3://bucket/path', boto3.Session())

        """
        super(ImageS3Dataset, self).__init__(path, suffix, boto3_session)

    def _data_fn(self, data):
        image = Image.open(data)
        tensor = to_tensor(image)
        return tensor


class S3IterableDataset(_BaseS3Dataset, IterableDataset):
    """PyTorch Amazon S3 Iterable Dataset."""

    def __init__(
        self,
        path: Union[str, List[str]],
        suffix: Optional[str] = None,
        boto3_session: Optional[boto3.Session] = None,
    ):
        """PyTorch Amazon S3 Iterable Dataset.

        Parameters
        ----------
        path : Union[str, List[str]]
            S3 prefix (e.g. s3://bucket/prefix) or list of S3 objects paths (e.g. [s3://bucket/key0, s3://bucket/key1]).
        boto3_session : boto3.Session(), optional
            Boto3 Session. The default boto3 session will be used if boto3_session receive None.

        Returns
        -------
        torch.utils.data.Dataset

        """
        super(S3IterableDataset, self).__init__(path, suffix, boto3_session)
        self._paths_index = 0

    def __iter__(self) -> Union[Iterator[torch.Tensor], Iterator[Tuple[torch.Tensor, torch.Tensor]]]:
        for path in self._paths:
            data = self._fetch_data(path)
            data = self._load_data(data, path)
            for d in data:
                yield d


class SQLDataset(IterableDataset):  # pylint: disable=too-few-public-methods,abstract-method
    """Pytorch Iterable SQL Dataset."""

    def __init__(
        self,
        sql: str,
        con: sqlalchemy.engine.Engine,
        label_col: Optional[Union[int, str]] = None,
        chunksize: Optional[int] = None,
    ):
        """Pytorch Iterable SQL Dataset.

        Support for **Redshift**, **PostgreSQL** and **MySQL**.

        Parameters
        ----------
        sql : str
            Pandas DataFrame https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.html
        con : sqlalchemy.engine.Engine
            SQLAlchemy Engine. Please use,
            wr.db.get_engine(), wr.db.get_redshift_temp_engine() or wr.catalog.get_engine()
        label_col : int, optional
            Label column number

        Returns
        -------
        torch.utils.data.dataset.IterableDataset

        Examples
        --------
        >>> import awswrangler as wr
        >>> con = wr.catalog.get_engine("aws-data-wrangler-postgresql")
        >>> ds = wr.torch.SQLDataset('select * from public.tutorial', con=con)

        """
        super().__init__()
        self._sql = sql
        self._con = con
        self._label_col = label_col
        self._chunksize = chunksize

    def __iter__(self) -> Union[Iterator[torch.Tensor], Iterator[Tuple[torch.Tensor, torch.Tensor]]]:
        """Iterate over the Dataset."""
        if torch.utils.data.get_worker_info() is not None:  # type: ignore
            raise NotImplementedError()
        db._validate_engine(con=self._con)  # pylint: disable=protected-access
        with self._con.connect() as con:
            cursor: Any = con.execute(self._sql)
            if (self._label_col is not None) and isinstance(self._label_col, str):
                label_col: Optional[int] = list(cursor.keys()).index(self._label_col)
            else:
                label_col = self._label_col
            _logger.debug(f"label_col: {label_col}")
            if self._chunksize is None:
                return SQLDataset._records2tensor(records=cursor.fetchall(), label_col=label_col)
            return self._iterate_cursor(cursor=cursor, chunksize=self._chunksize, label_col=label_col)

    @staticmethod
    def _iterate_cursor(
        cursor: Any, chunksize: int, label_col: Optional[int] = None
    ) -> Union[Iterator[torch.Tensor], Iterator[Tuple[torch.Tensor, torch.Tensor]]]:
        while True:
            records = cursor.fetchmany(chunksize)
            if not records:
                break
            yield from SQLDataset._records2tensor(records=records, label_col=label_col)

    @staticmethod
    def _records2tensor(
        records: List[Tuple[Any]], label_col: Optional[int] = None
    ) -> Union[Iterator[torch.Tensor], Iterator[Tuple[torch.Tensor, torch.Tensor]]]:  # pylint: disable=unused-argument
        for row in records:
            if label_col is None:
                arr_data: np.ndarray = np.array(row, dtype=np.float)
                yield torch.as_tensor(arr_data, dtype=torch.float)  # pylint: disable=no-member
            else:
                arr_data = np.array(row[:label_col] + row[label_col + 1 :], dtype=np.float)  # noqa: E203
                arr_label: np.ndarray = np.array(row[label_col], dtype=np.long)
                ts_data: torch.Tensor = torch.as_tensor(arr_data, dtype=torch.float)  # pylint: disable=no-member
                ts_label: torch.Tensor = torch.as_tensor(arr_label, dtype=torch.long)  # pylint: disable=no-member
                yield ts_data, ts_label
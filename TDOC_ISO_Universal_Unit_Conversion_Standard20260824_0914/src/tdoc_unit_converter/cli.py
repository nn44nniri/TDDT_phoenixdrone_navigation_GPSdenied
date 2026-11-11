import argparse, json
from .converter import UnitConverter

def main():
    p=argparse.ArgumentParser(description="TDOC v2 strict unit converter")
    p.add_argument("value", type=float); p.add_argument("from_unit"); p.add_argument("to_unit"); p.add_argument("--quantity")
    a=p.parse_args(); r=UnitConverter().convert(a.value,a.from_unit,a.to_unit,quantity_kind=a.quantity)
    print(json.dumps(r.__dict__,ensure_ascii=False,indent=2))
if __name__=="__main__": main()

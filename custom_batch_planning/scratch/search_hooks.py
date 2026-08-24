import os

def main():
    path = "/home/shivam/frappe-bench/frappe-bench/apps/erpnext/erpnext/hooks.py"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        print("Hook fields_for_group_similar_items:")
        found = False
        for idx, line in enumerate(lines, 1):
            if "fields_for_group_similar_items" in line:
                found = True
            if found:
                print(f"{idx}: {line.strip()}")
                if "]" in line and idx > lines.index(line):
                    break
    else:
        print("hooks.py not found")

if __name__ == "__main__":
    main()

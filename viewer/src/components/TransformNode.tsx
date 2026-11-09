import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { memo } from "react";
import type { BoxData, RowState } from "../dfgraph";

export type TransformNodeData = {
  box: BoxData;
  rows?: Map<string, RowState>;
  selected: boolean;
  dim: boolean;
  onBox: (box: BoxData) => void;
  onCol: (box: BoxData, node: string) => void;
};

export type TransformNodeType = Node<TransformNodeData, "transform">;

export const TransformNode = memo(function TransformNode({ data }: NodeProps<TransformNodeType>) {
  const { box, rows, selected, dim, onBox, onCol } = data;
  return (
    <div className={`tnode ${box.kindClass}${selected ? " selected" : ""}${dim ? " dim" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <div
        className="tnode-head"
        role="button"
        tabIndex={0}
        onClick={() => onBox(box)}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onBox(box)}
        aria-label={`${box.kindLabel} ${box.name}`}
      >
        <div className="kind">{box.kindLabel}</div>
        <div className="tname">{box.name}</div>
        {box.table && box.table !== box.name && <div className="table">{box.table}</div>}
      </div>
      {box.cols.length > 0 && (
        <div className="tnode-cols nowheel">
          {box.cols.map((c) => (
            <button
              key={c.label}
              className={`tnode-col ${rows?.get(c.label) ?? ""}`}
              onClick={() => onCol(box, c.node)}
              title={`Trace ${c.node}`}
            >
              <span>{c.label}</span>
              {c.type && <span className="t">{c.type}</span>}
            </button>
          ))}
        </div>
      )}
      <Handle type="source" position={Position.Right} />
    </div>
  );
});
